import { useState } from 'react'
import type { BatchDetail } from '../api'
import { formatBytes, formatTime, shortDigest } from '../format'

interface Props {
  batch: BatchDetail
  onUpload: (file: File, relPath: string) => Promise<void>
  onSeal: () => Promise<void>
  onPublish: () => Promise<void>
  onCancel: () => Promise<void>
  onRetry: () => Promise<void>
  busy: string | null
  error: string | null
}

export function BatchPanel({
  batch,
  onUpload,
  onSeal,
  onPublish,
  onCancel,
  onRetry,
  busy,
  error,
}: Props) {
  const [queued, setQueued] = useState<File[]>([])
  const declared = batch.declarations
  const uploaded = new Map(batch.files.map((f) => [f.rel_path, f]))
  const missing = declared.filter((d) => !uploaded.has(d.rel_path))
  const canSeal = batch.phase === 'preparing' && missing.length === 0 && !busy
  const canPublish = batch.phase === 'file_ready' && !busy
  const canCancel = batch.phase !== 'published' && !batch.failure_code && !busy
  const isFailed = Boolean(batch.failure_code) && batch.phase !== 'published'

  const pickFiles = (files: FileList | null) => {
    if (!files) return
    setQueued(Array.from(files))
  }

  const uploadQueued = async () => {
    for (const file of queued) {
      // Backend keys files by relative path.  The browser exposes a path
      // when the user picked a folder; otherwise match by bare name.
      const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
      const match =
        declared.find((d) => d.rel_path === relative) ??
        declared.find((d) => d.rel_path.endsWith('/' + relative)) ??
        declared.find((d) => d.rel_path.split('/').pop() === file.name)
      await onUpload(file, match?.rel_path ?? relative)
    }
    setQueued([])
  }

  return (
    <article className="panel">
      <header className="panel-head">
        <div>
          <h2>{batch.batch_id}</h2>
          <p className="sub mono">uid {batch.uid} · attempt {batch.attempt}</p>
        </div>
        <span className={`chip phase-${batch.phase}`}>
          {batch.phase === 'published' ? '正式可见' : batch.phase === 'file_ready' ? '文件就绪' : '准备中'}
        </span>
      </header>

      {isFailed && (
        <div className="callout danger" role="alert">
          <strong>{batch.cancelled_at ? '已取消' : '发布失败'}</strong>
          <span>{batch.failure_reason}</span>
          {batch.cancelled_at && <span className="muted">取消于 {formatTime(batch.cancelled_at)}，仅清理本批次拥有的文件。</span>}
        </div>
      )}
      {error && (
        <div className="callout warn" role="alert">
          {error}
        </div>
      )}

      <section className="manifest">
        <h3>文件清单</h3>
        <table>
          <thead>
            <tr>
              <th>相对路径</th>
              <th>声明大小</th>
              <th>已登记</th>
              <th>散列</th>
              <th>位置</th>
            </tr>
          </thead>
          <tbody>
            {declared.map((item) => {
              const file = uploaded.get(item.rel_path)
              return (
                <tr key={item.rel_path} className={file ? '' : 'pending-row'}>
                  <td className="mono">{item.rel_path}</td>
                  <td>{formatBytes(item.declared_size)}</td>
                  <td>{file ? formatBytes(file.size) : <span className="muted">未上传</span>}</td>
                  <td className="mono small">{file ? shortDigest(file.sha256) : '—'}</td>
                  <td>{file ? (file.state === 'moved' ? '正式目录' : '暂存目录') : '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </section>

      {batch.phase === 'preparing' && (
        <section className="controls">
          <h3>上传数据文件</h3>
          <p className="muted">
            文件先写入暂存目录，逐份登记大小与 SHA-256；全部齐备后才能封存。
          </p>
          <input
            type="file"
            multiple
            onChange={(event) => pickFiles(event.target.files)}
            disabled={Boolean(busy)}
            aria-label="选择数据文件"
          />
          {queued.length > 0 && (
            <div className="queued">
              <span>{queued.length} 个文件待上传</span>
              <button onClick={uploadQueued} disabled={Boolean(busy)}>
                {busy === 'upload' ? '上传中…' : '开始上传'}
              </button>
            </div>
          )}
          {missing.length > 0 && (
            <p className="muted small">还需上传：{missing.map((m) => m.rel_path).join('、')}</p>
          )}
        </section>
      )}

      <section className="actions">
        <button onClick={onSeal} disabled={!canSeal} title={missing.length ? '尚有文件未上传' : ''}>
          封存文件（进入文件就绪）
        </button>
        <button onClick={onPublish} disabled={!canPublish} className="primary">
          发布（移入正式目录）
        </button>
        {isFailed && (
          <button onClick={onRetry} disabled={Boolean(busy)}>
            重新提供数据（新尝试）
          </button>
        )}
        <button onClick={onCancel} disabled={!canCancel} className="ghost">
          取消并清理
        </button>
      </section>

      {batch.publish_requested_at && batch.phase === 'preparing' && (
        <div className="callout info">
          已记录发布请求（{formatTime(batch.publish_requested_at)}）。封存完成后将自动继续发布。
        </div>
      )}

      {batch.diagnostics.length > 0 && (
        <section className="diagnostics">
          <h3>诊断记录</h3>
          <ul>
            {batch.diagnostics.map((d, index) => (
              <li key={`${d.code}-${d.rel_path}-${index}`}>
                <code>{d.code}</code>
                <span className="mono">{d.rel_path || '批次'}</span>
                <span>{d.detail}</span>
                <time>{formatTime(d.created_at)}</time>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="snapshots">
        <h3>阶段快照</h3>
        {batch.snapshots.length === 0 ? (
          <p className="muted">尚未写入快照。</p>
        ) : (
          <ul>
            {batch.snapshots.map((s) => (
              <li key={s.stage}>
                <span className="badge">{s.stage}</span>
                <span>{s.file_count} 个文件 · {formatBytes(s.total_bytes)}</span>
                <code className="small">{shortDigest(s.content_digest)}</code>
                <time>{formatTime(s.created_at)}</time>
              </li>
            ))}
          </ul>
        )}
      </section>
    </article>
  )
}
