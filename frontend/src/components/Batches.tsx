import { useState } from 'react'
import { api, ApiError, type BatchSummary } from '../api'
import { formatBytes, formatTime, shortDigest } from '../format'

interface Props {
  onCreated: (batchId: string) => Promise<void>
  onOpen: (batchId: string) => void
}

export function CreateBatch({ onCreated, onOpen }: Props) {
  const [batchId, setBatchId] = useState('')
  const [rows, setRows] = useState<{ rel_path: string; size: string }[]>([{ rel_path: '', size: '' }])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const updateRow = (index: number, key: 'rel_path' | 'size', value: string) => {
    setRows((current) => current.map((row, i) => (i === index ? { ...row, [key]: value } : row)))
  }

  const submit = async () => {
    setError(null)
    const files = rows
      .map((row) => ({ rel_path: row.rel_path.trim(), size: Number(row.size) }))
      .filter((row) => row.rel_path.length > 0)
    if (!batchId.trim()) return setError('请填写批次编号')
    if (files.length === 0) return setError('至少声明一个数据文件')
    if (files.some((file) => !Number.isInteger(file.size) || file.size < 0)) {
      return setError('文件大小必须是非负整数（字节）')
    }
    setBusy(true)
    try {
      const created = await api.create(batchId.trim(), files)
      await onCreated(created.batch_id)
      onOpen(created.batch_id)
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}：${err.message}` : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="create-card">
      <h2>登记新批次</h2>
      <p className="muted">
        声明每个文件的相对路径与字节数。登记完成后逐一上传，登记时即可发现编号冲突。
      </p>
      <label>
        批次编号
        <input
          value={batchId}
          onChange={(event) => setBatchId(event.target.value)}
          placeholder="例如 2026-11-annual-report"
          disabled={busy}
        />
      </label>
      <div className="declared-rows">
        {rows.map((row, index) => (
          <div className="declared-row" key={index}>
            <input
              value={row.rel_path}
              onChange={(event) => updateRow(index, 'rel_path', event.target.value)}
              placeholder="data/report.csv"
              disabled={busy}
              aria-label={`文件 ${index + 1} 相对路径`}
            />
            <input
              type="number"
              min={0}
              value={row.size}
              onChange={(event) => updateRow(index, 'size', event.target.value)}
              placeholder="字节"
              disabled={busy}
              aria-label={`文件 ${index + 1} 大小`}
            />
            <button
              type="button"
              className="ghost"
              onClick={() => setRows((current) => current.filter((_, i) => i !== index))}
              disabled={busy || rows.length === 1}
              aria-label="删除该行"
            >
              ×
            </button>
          </div>
        ))}
      </div>
      <div className="declared-actions">
        <button type="button" className="ghost" onClick={() => setRows((c) => [...c, { rel_path: '', size: '' }])} disabled={busy}>
          + 添加文件
        </button>
        <button type="button" className="primary" onClick={submit} disabled={busy}>
          登记
        </button>
      </div>
      {error && <div className="callout danger">{error}</div>}
    </section>
  )
}

export function BatchTable({ batches, onOpen }: { batches: BatchSummary[]; onOpen: (id: string) => void }) {
  if (batches.length === 0) {
    return <p className="muted empty">还没有批次。登记一个批次后，上传文件即可开始发布流程。</p>
  }
  return (
    <table className="batch-table">
      <thead>
        <tr>
          <th>批次编号</th>
          <th>阶段</th>
          <th>文件</th>
          <th>内容摘要</th>
          <th>更新时间</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {batches.map((batch) => (
          <tr key={batch.uid} className={batch.failure_code ? 'failed-row' : ''}>
            <td>
              <button className="link" onClick={() => onOpen(batch.batch_id)}>
                {batch.batch_id}
              </button>
              {batch.attempt > 0 && <span className="badge">尝试 {batch.attempt}</span>}
            </td>
            <td>
              <span className={`chip phase-${batch.phase}`}>
                {batch.phase === 'published' ? '正式可见' : batch.phase === 'file_ready' ? '文件就绪' : '准备中'}
              </span>
              {batch.failure_code && <span className="chip danger">{batch.failure_code === 'cancelled' ? '已取消' : '失败'}</span>}
            </td>
            <td>{batch.file_count} · {formatBytes(batch.total_bytes)}</td>
            <td className="mono small">{shortDigest(batch.content_digest)}</td>
            <td>{formatTime(batch.updated_at)}</td>
            <td>
              {batch.downloadable && (
                <a href={`/api/downloads/${encodeURIComponent(batch.batch_id)}`} className="chip ok">
                  下载
                </a>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function useRefresh() {
  const [tick, setTick] = useState(0)
  return { tick, refresh: () => setTick((value) => value + 1) }
}
