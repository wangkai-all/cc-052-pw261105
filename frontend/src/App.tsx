import { useEffect, useMemo, useState } from 'react'
import {
  api,
  ApiError,
  type BatchDetail,
  type BatchSummary,
  type DownloadIndex,
  type Health,
  type RecoveryResult,
  type RecoveryRun,
} from './api'
import { BatchPanel } from './components/BatchPanel'
import { BatchTable, CreateBatch } from './components/Batches'
import { PhaseTimeline } from './components/PhaseTimeline'
import { RecoveryBanner, RecoveryHistory } from './components/Recovery'
import { formatBytes, formatTime, shortDigest } from './format'

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [batches, setBatches] = useState<BatchSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<BatchDetail | null>(null)
  const [downloads, setDownloads] = useState<DownloadIndex | null>(null)
  const [recovery, setRecovery] = useState<RecoveryResult | null>(null)
  const [history, setHistory] = useState<RecoveryRun[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const loadList = async () => {
    const [list, runs] = await Promise.all([api.batches(), api.recoveries()])
    setBatches(list)
    setHistory(runs)
    if (!selectedId && list.length > 0) setSelectedId(list[0].batch_id)
  }

  const loadDetail = async (batchId: string) => {
    const detail = await api.batch(batchId)
    setDetail(detail)
    if (detail.downloadable) {
      try {
        setDownloads(await api.downloadIndex(batchId))
      } catch {
        setDownloads(null)
      }
    } else {
      setDownloads(null)
    }
    return detail
  }

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const h = await api.health()
        if (cancelled) return
        setHealth(h)
        setRecovery(h.startup_recovery)
        await loadList()
      } catch (err) {
        if (!cancelled) setError(describe(err))
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    let cancelled = false
    ;(async () => {
      try {
        const d = await api.batch(selectedId)
        if (!cancelled) {
          setDetail(d)
          if (d.downloadable) {
            try {
              setDownloads(await api.downloadIndex(selectedId))
            } catch {
              setDownloads(null)
            }
          } else {
            setDownloads(null)
          }
        }
      } catch (err) {
        if (!cancelled) setError(describe(err))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [selectedId])

  const withBusy = async (label: string, action: () => Promise<void>) => {
    setBusy(label)
    setError(null)
    setNotice(null)
    try {
      await action()
      await loadList()
      if (selectedId) await loadDetail(selectedId)
    } catch (err) {
      setError(describe(err))
    } finally {
      setBusy(null)
    }
  }

  const handleCreated = async (batchId: string) => {
    await loadList()
    setSelectedId(batchId)
  }

  const runRecovery = () =>
    withBusy('recover', async () => {
      const result = await api.recover()
      setRecovery(result)
      setNotice(
        `恢复完成：检查 ${result.summary.checked} 个批次，继续发布 ${result.summary.published} 个，失败 ${result.summary.failed} 个。`,
      )
    })

  const activeDownloads = useMemo(() => downloads?.files ?? [], [downloads])

  return (
    <div className="app">
      <header className="app-head">
        <div>
          <h1>文件发布台</h1>
          <p className="muted">
            文件先进入暂存目录，校验后原子移入正式目录；只有正式可见的批次会开放下载。
          </p>
        </div>
        <div className="app-actions">
          <button onClick={runRecovery} disabled={Boolean(busy)}>
            {busy === 'recover' ? '核对中…' : '立即核对恢复'}
          </button>
          {health && (
            <span className={`health ${health.ok ? 'ok' : 'bad'}`}>
              暂存：{health.staging_dir} · 正式：{health.published_dir}
            </span>
          )}
        </div>
      </header>

      {recovery && <RecoveryBanner result={recovery} />}
      {notice && <div className="callout info">{notice}</div>}
      {error && (
        <div className="callout danger" role="alert">
          {error}
        </div>
      )}

      <main className="layout">
        <aside className="sidebar">
          <CreateBatch onCreated={handleCreated} onOpen={setSelectedId} />
          <section className="list">
            <h2>批次列表</h2>
            <BatchTable batches={batches} onOpen={setSelectedId} />
          </section>
          <RecoveryHistory runs={history} />
        </aside>

        <section className="main-pane">
          {!detail ? (
            <p className="muted empty">从左侧选择一个批次查看详情。</p>
          ) : (
            <>
              <PhaseTimeline
                phase={detail.phase}
                publishState={detail.publish_state}
                failed={Boolean(detail.failure_code) && detail.phase !== 'published'}
                cancelled={Boolean(detail.cancelled_at)}
                digest={detail.content_digest}
              />
              <BatchPanel
                batch={detail}
                busy={busy}
                error={null}
                onUpload={(file, relPath) =>
                  withBusy('upload', async () => {
                    await api.upload(detail.batch_id, relPath, file)
                  })
                }
                onSeal={() => withBusy('seal', async () => void (await api.seal(detail.batch_id)))}
                onPublish={() =>
                  withBusy('publish', async () => {
                    await api.publish(detail.batch_id)
                    setNotice('批次已正式可见，下载入口已开放。')
                  })
                }
                onCancel={() =>
                  withBusy('cancel', async () => {
                    const result = await api.cancel(detail.batch_id)
                    setNotice(`已取消，清理了 ${result.removed_files} 个自有文件。`)
                  })
                }
                onRetry={() => withBusy('retry', async () => void (await api.retry(detail.batch_id)))}
              />

              {detail.phase === 'published' && (
                <section className="downloads">
                  <h3>下载（仅正式可见内容）</h3>
                  {activeDownloads.length === 0 ? (
                    <p className="muted">正在核对文件完整性…</p>
                  ) : (
                    <ul>
                      {activeDownloads.map((file) => (
                        <li key={file.rel_path}>
                          <a href={file.url} download data-sha={file.sha256}>
                            {file.rel_path}
                          </a>
                          <span>{formatBytes(file.size)}</span>
                          <code className="small">sha256 {shortDigest(file.sha256)}</code>
                        </li>
                      ))}
                    </ul>
                  )}
                  {'content_digest' in (downloads ?? {}) && downloads && (
                    <p className="muted small">
                      批次摘要 <code>{downloads.content_digest}</code> · 发布于 {formatTime(downloads.published_at)}
                    </p>
                  )}
                </section>
              )}
            </>
          )}
        </section>
      </main>
    </div>
  )
}

function describe(err: unknown): string {
  if (err instanceof ApiError) return `${err.code}：${err.message}`
  return String(err)
}
