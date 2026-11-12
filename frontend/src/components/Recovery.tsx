import type { RecoveryResult, RecoveryRun } from '../api'
import { formatTime } from '../format'

const ACTION_LABELS: Record<string, string> = {
  published: '已继续发布',
  failed: '失败（已诊断）',
  error: '异常',
  waiting: '保持不变',
  cancelled: '已取消',
  none: '无需处理',
}

export function RecoveryBanner({ result }: { result: RecoveryResult }) {
  const { summary, batches } = result
  const noteworthy = batches.filter((b) => b.action !== 'none' && b.action !== 'waiting')
  return (
    <section className={`recovery-banner ${summary.failed > 0 ? 'has-failures' : 'clean'}`}>
      <div className="recovery-head">
        <strong>启动恢复检查</strong>
        <span>
          检查 {summary.checked} 个未完成批次：继续发布 {summary.published} 个，失败 {summary.failed} 个，保持不变 {summary.left_alone} 个。
        </span>
        <time>{formatTime(summary.finished_at)}</time>
      </div>
      {noteworthy.length > 0 && (
        <ul>
          {noteworthy.map((batch) => (
            <li key={batch.uid} className={batch.action === 'failed' || batch.action === 'error' ? 'danger' : ''}>
              <span className="badge">{ACTION_LABELS[batch.action] ?? batch.action}</span>
              <button className="link" onClick={() => navigator.clipboard?.writeText(batch.batch_id)} title={batch.uid}>
                {batch.batch_id}
              </button>
              <span>{batch.reason}</span>
              {batch.diagnostics.length > 0 && (
                <span className="muted small">
                  诊断 {batch.diagnostics.length} 条，例如 {batch.diagnostics[0].code}（{batch.diagnostics[0].rel_path || '批次'}）
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

export function RecoveryHistory({ runs }: { runs: RecoveryRun[] }) {
  if (runs.length === 0) return null
  return (
    <section className="history">
      <h2>恢复记录</h2>
      <ul>
        {runs.map((run) => (
          <li key={run.id}>
            <time>{formatTime(run.started_at)}</time>
            <span>
              检查 {run.batches_checked} · 继续 {run.published_resumed} · 失败 {run.summary.failed} · 保持不变 {run.summary.left_alone}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
