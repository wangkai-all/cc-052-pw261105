import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { api, ApiError, type BatchDetail, type BatchSummary, type Health } from './api'

const batch: BatchDetail = {
  uid: 'u-1',
  batch_id: 'q3-data',
  attempt: 0,
  phase: 'published',
  publish_state: 'committed',
  content_digest: 'd'.repeat(64),
  failure_code: null,
  failure_reason: null,
  created_at: '2026-11-12T10:00:00.000Z',
  updated_at: '2026-11-12T10:01:00.000Z',
  publish_requested_at: null,
  cancelled_at: null,
  failed_at: null,
  declarations: [
    { rel_path: 'reports/q3.csv', declared_size: 12 },
    { rel_path: 'raw/events.jsonl', declared_size: 20 },
  ],
  files: [
    { rel_path: 'reports/q3.csv', size: 12, sha256: 'a'.repeat(64), state: 'moved' },
    { rel_path: 'raw/events.jsonl', size: 20, sha256: 'b'.repeat(64), state: 'moved' },
  ],
  snapshots: [
    { stage: 'staged', content_digest: 'd'.repeat(64), file_count: 2, total_bytes: 32, created_at: '2026-11-12T10:00:30.000Z' },
    { stage: 'file_ready', content_digest: 'd'.repeat(64), file_count: 2, total_bytes: 32, created_at: '2026-11-12T10:00:40.000Z' },
    { stage: 'moved', content_digest: 'd'.repeat(64), file_count: 2, total_bytes: 32, created_at: '2026-11-12T10:01:00.000Z' },
    { stage: 'published', content_digest: 'd'.repeat(64), file_count: 2, total_bytes: 32, created_at: '2026-11-12T10:01:00.000Z' },
  ],
  diagnostics: [],
  downloadable: true,
  file_count: 2,
  total_bytes: 32,
}

const summary: BatchSummary = { ...batch }

const health: Health = {
  ok: true,
  staging_dir: '/tmp/staging',
  published_dir: '/tmp/published',
  database: '/tmp/desk.sqlite3',
  crash_points: [],
  startup_recovery: {
    summary: { checked: 1, published: 0, failed: 1, left_alone: 0, started_at: '', finished_at: '' },
    batches: [
      { uid: 'u-2', batch_id: 'broken', action: 'failed', reason: 'published copy corrupt', diagnostics: [{ code: 'published_hash_mismatch', rel_path: 'x', detail: '', created_at: '' }] },
    ],
  },
}

function mockFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn(handler)
  vi.stubGlobal('fetch', fn)
  return fn
}

const defaultHandler = (url: string): Response => {
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  if (url === '/api/health') return json(health)
  if (url === '/api/batches') return json([summary])
  if (url === '/api/recoveries') return json([])
  if (url === '/api/batches/q3-data') return json(batch)
  if (url === '/api/downloads/q3-data') {
    return json({
      batch_id: 'q3-data', uid: 'u-1', phase: 'published', content_digest: 'd'.repeat(64),
      published_at: '2026-11-12T10:01:00.000Z',
      files: batch.files.map((f) => ({ ...f, url: `/api/downloads/q3-data/files/${f.rel_path}` })),
    })
  }
  return new Response(JSON.stringify({ error: 'not_found', message: url }), { status: 404 })
}

describe('release desk UI', () => {
  beforeEach(() => mockFetch(defaultHandler))

  it('shows the startup recovery banner with diagnosed failures', async () => {
    render(<App />)
    const banner = await screen.findByText('启动恢复检查')
    expect(banner).toBeInTheDocument()
    expect(await screen.findByText(/失败（已诊断）/)).toBeInTheDocument()
    expect(await screen.findByText(/published_hash_mismatch/)).toBeInTheDocument()
  })

  it('renders the three-stage timeline and only exposes downloads when published', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'q3-data' })).toBeInTheDocument())

    for (const label of ['准备中', '文件就绪', '正式可见']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0)
    }

    const downloads = await screen.findByText(/下载（仅正式可见内容）/i)
    const list = downloads.closest('section') as HTMLElement
    const links = within(list).getAllByRole('link')
    expect(links.map((a) => a.getAttribute('href'))).toEqual([
      '/api/downloads/q3-data/files/reports/q3.csv',
      '/api/downloads/q3-data/files/raw/events.jsonl',
    ])
    expect(list.textContent).toContain('sha256')
  })

  it('disables publish until files are sealed, and blocks cancel after publication', async () => {
    render(<App />)
    await screen.findByRole('heading', { name: 'q3-data' })
    const publish = screen.getByRole('button', { name: '发布（移入正式目录）' })
    const cancel = screen.getByRole('button', { name: '取消并清理' })
    expect(publish).toBeDisabled()
    expect(cancel).toBeDisabled()
  })

  it('shows declared-but-unuploaded files as pending', async () => {
    const preparing: BatchDetail = {
      ...batch,
      phase: 'preparing',
      publish_state: 'collecting',
      files: [],
      snapshots: [],
      downloadable: false,
    }
    mockFetch((url) => {
      const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (url === '/api/health') return json({ ...health, startup_recovery: null })
      if (url === '/api/batches') return json([{ ...preparing, file_count: 0, total_bytes: 0 }])
      if (url === '/api/recoveries') return json([])
      if (url === '/api/batches/q3-data') return json(preparing)
      return new Response('{}', { status: 404 })
    })

    render(<App />)
    await screen.findByRole('heading', { name: 'q3-data' })
    expect(screen.getAllByText('未上传').length).toBe(2)
    expect(screen.getByRole('button', { name: '封存文件（进入文件就绪）' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '取消并清理' })).toBeEnabled()
  })

  it('surfaces API errors from create as readable text', async () => {
    mockFetch((url) => {
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      if (url === '/api/health') return json({ ...health, startup_recovery: null })
      if (url === '/api/batches') return json([])
      if (url === '/api/recoveries') return json([])
      return new Response('{}', { status: 404 })
    })
    vi.spyOn(api, 'create').mockRejectedValue(
      new ApiError(409, 'content_conflict', 'batch id already names a different file set'),
    )

    const user = userEvent.setup()
    render(<App />)
    await user.type(screen.getByLabelText('批次编号'), 'q3-data')
    await user.type(screen.getByLabelText('文件 1 相对路径'), 'a.csv')
    await user.type(screen.getByLabelText('文件 1 大小'), '10')
    await user.click(screen.getByRole('button', { name: '登记' }))
    expect(await screen.findByText(/content_conflict/)).toBeInTheDocument()
    expect(await screen.findByText(/different file set/)).toBeInTheDocument()
  })
})
