import type { Phase, PublishState } from '../api'
import { PHASE_COPY, PHASE_LABELS, PUBLISH_STATE_LABELS } from '../format'

const STEPS: { phase: Phase; title: string; detail: string }[] = [
  { phase: 'preparing', title: '准备中', detail: '写入暂存目录 / 记录散列' },
  { phase: 'file_ready', title: '文件就绪', detail: '全部文件校验并封存' },
  { phase: 'published', title: '正式可见', detail: '移入正式目录 / 提交登记' },
]

const ORDER: Record<Phase, number> = { preparing: 0, file_ready: 1, published: 2 }

interface Props {
  phase: Phase
  publishState: PublishState
  failed: boolean
  cancelled: boolean
  digest: string | null
}

export function PhaseTimeline({ phase, publishState, failed, cancelled, digest }: Props) {
  const current = ORDER[phase]
  return (
    <section className="timeline" aria-label="发布阶段">
      {STEPS.map((step, index) => {
        const complete = index < current || (index === current && phase === 'published' && publishState === 'committed')
        const active = index === current && !(phase === 'published')
        const state = complete ? 'complete' : active ? 'active' : 'pending'
        return (
          <div key={step.phase} className={`timeline-step ${state}`}>
            <div className="timeline-marker" aria-hidden="true">
              {complete ? '✓' : index + 1}
            </div>
            <div className="timeline-body">
              <div className="timeline-title">{step.title}</div>
              <div className="timeline-detail">{step.detail}</div>
              {step.phase === phase && (
                <div className="timeline-meta">
                  {PHASE_COPY[phase]}
                  {' · '}
                  <span className="mono">{PUBLISH_STATE_LABELS[publishState] ?? publishState}</span>
                </div>
              )}
            </div>
          </div>
        )
      })}
      {failed && <div className="timeline-flag danger">恢复时判定失败{cancelled ? '（已取消）' : ''}</div>}
      {digest && !failed && (
        <div className="timeline-digest">
          <span className="label">内容摘要</span>
          <code>{digest}</code>
        </div>
      )}
    </section>
  )
}

export function phaseHeadline(phase: Phase, publishState: PublishState): string {
  if (phase === 'published') {
    return publishState === 'committed' ? '正式可见 · 已提交' : '正式可见'
  }
  return PHASE_LABELS[phase]
}
