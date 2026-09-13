// FastAPI 后端 API 客户端
import type {
  RunsResp,
  RunDetail,
  TradesResp,
  SourceResp,
} from './types'

const BASE = '/api'

async function req<T>(path: string): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { cache: 'no-store' })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json() as Promise<T>
}

export const api = {
  ping: () => req<{ ok: boolean; root: string }>('/ping'),
  runs: () => req<RunsResp>('/runs'),
  detail: (name: string) =>
    req<RunDetail>(`/run/${encodeURIComponent(name)}/detail`),
  trades: (name: string, page = 1, pageSize = 20) =>
    req<TradesResp>(
      `/run/${encodeURIComponent(name)}/trades?page=${page}&page_size=${pageSize}`,
    ),
  log: (name: string, lines = 50, offset = 0) =>
    req<{ lines: string[]; total?: number }>(
      `/run/${encodeURIComponent(name)}/log?lines=${lines}&offset=${offset}`,
    ),
  source: (name: string) =>
    req<SourceResp>(`/run/${encodeURIComponent(name)}/source`),
}
