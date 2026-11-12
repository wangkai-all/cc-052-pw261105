export type Phase = 'preparing' | 'file_ready' | 'published'
export type PublishState = 'collecting' | 'ready' | 'moving' | 'committed'
export type FileState = 'staged' | 'moved'

export interface DeclaredFile {
  rel_path: string
  declared_size: number
}

export interface BatchFile {
  rel_path: string
  size: number
  sha256: string
  state: FileState
}

export interface Snapshot {
  stage: 'staged' | 'file_ready' | 'moved' | 'published'
  content_digest: string
  file_count: number
  total_bytes: number
  created_at: string
}

export interface Diagnostic {
  code: string
  rel_path: string
  detail: string
  created_at: string
}

export interface BatchSummary {
  uid: string
  batch_id: string
  attempt: number
  phase: Phase
  publish_state: PublishState
  content_digest: string | null
  failure_code: string | null
  failure_reason: string | null
  created_at: string
  updated_at: string
  file_count: number
  total_bytes: number
  downloadable: boolean
}

export interface BatchDetail extends BatchSummary {
  publish_requested_at: string | null
  cancelled_at: string | null
  failed_at: string | null
  declarations: DeclaredFile[]
  files: BatchFile[]
  snapshots: Snapshot[]
  diagnostics: Diagnostic[]
}

export interface DownloadIndex {
  batch_id: string
  uid: string
  phase: Phase
  content_digest: string
  published_at: string
  files: (BatchFile & { url: string })[]
}

export interface RecoveryOutcome {
  uid: string
  batch_id: string
  action: 'published' | 'failed' | 'error' | 'waiting' | 'cancelled' | 'none'
  reason: string
  code?: string
  diagnostics: Diagnostic[]
  phase?: Phase
  file_count?: number
}

export interface RecoverySummary {
  checked: number
  published: number
  failed: number
  left_alone: number
  started_at: string
  finished_at: string
}

export interface RecoveryResult {
  summary: RecoverySummary
  batches: RecoveryOutcome[]
}

export interface RecoveryRun {
  id: number
  started_at: string
  finished_at: string
  batches_checked: number
  published_resumed: number
  summary: RecoverySummary
}

export interface Health {
  ok: boolean
  staging_dir: string
  published_dir: string
  database: string
  crash_points: string[]
  startup_recovery: RecoveryResult | null
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  const text = await response.text()
  const body = text ? safeJson(text) : null
  if (!response.ok) {
    const code = (body as { error?: string } | null)?.error ?? `http_${response.status}`
    const message =
      (body as { message?: string } | null)?.message ?? `${response.status} ${response.statusText}`
    throw new ApiError(response.status, code, message)
  }
  return body as T
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return { message: text }
  }
}

export const api = {
  health: () => request<Health>('/api/health'),
  batches: () => request<BatchSummary[]>('/api/batches'),
  batch: (batchId: string) => request<BatchDetail>(`/api/batches/${encodeURIComponent(batchId)}`),
  create: (batchId: string, files: { rel_path: string; size: number }[]) =>
    request<BatchDetail & { created: boolean; upload_url: string }>('/api/batches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ batch_id: batchId, files }),
    }),
  upload: (batchId: string, relPath: string, file: File) => {
    const form = new FormData()
    form.append('rel_path', relPath)
    form.append('file', file, file.name)
    return request<{ uploaded: BatchFile }>(`/api/batches/${encodeURIComponent(batchId)}/files`, {
      method: 'POST',
      body: form,
    })
  },
  seal: (batchId: string) =>
    request<{ phase: Phase; content_digest: string }>(
      `/api/batches/${encodeURIComponent(batchId)}/seal`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
    ),
  publish: (batchId: string) =>
    request<{ batch_id: string; download_url: string }>(
      `/api/batches/${encodeURIComponent(batchId)}/publish`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
    ),
  cancel: (batchId: string) =>
    request<{ batch_id: string; cancelled: boolean; removed_files: number }>(
      `/api/batches/${encodeURIComponent(batchId)}/cancel`,
      { method: 'POST' },
    ),
  retry: (batchId: string) =>
    request<BatchDetail>(`/api/batches/${encodeURIComponent(batchId)}/retry`, { method: 'POST' }),
  recover: () => request<RecoveryResult>('/api/recover', { method: 'POST' }),
  recoveries: () => request<RecoveryRun[]>('/api/recoveries'),
  downloadIndex: (batchId: string) =>
    request<DownloadIndex>(`/api/downloads/${encodeURIComponent(batchId)}`),
}
