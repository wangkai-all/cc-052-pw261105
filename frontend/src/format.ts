export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function shortDigest(digest: string | null | undefined): string {
  if (!digest) return '—'
  return `${digest.slice(0, 12)}…${digest.slice(-6)}`
}

export const PHASE_LABELS: Record<string, string> = {
  preparing: '准备中',
  file_ready: '文件就绪',
  published: '正式可见',
}

export const PHASE_COPY: Record<string, string> = {
  preparing: '正在写入暂存目录并登记散列',
  file_ready: '所有文件已校验封存，等待移入正式目录',
  published: '已移入正式目录并提交登记，可下载',
}

export const PUBLISH_STATE_LABELS: Record<string, string> = {
  collecting: '收集文件',
  ready: '待发布',
  moving: '移动文件中',
  committed: '已提交',
}
