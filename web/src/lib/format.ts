// dayjs 时间与数值格式化
import dayjs from 'dayjs'

export function isNum(v: unknown): v is number {
  return typeof v === 'number' && !Number.isNaN(v)
}

/** 涨跌颜色类：正红 负绿 */
export function pCls(v: unknown): string {
  if (!isNum(v)) return ''
  return v >= 0 ? 'text-destructive' : 'text-emerald-600'
}

export function fmtPct(v: unknown, nd = 2): string {
  return isNum(v) ? `${(v * 100).toFixed(nd)}%` : '—'
}

export function fmtNum(v: unknown, nd = 2): string {
  return isNum(v) ? v.toFixed(nd) : '—'
}

export function fmtBig(v: unknown): string {
  return isNum(v)
    ? v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
    : '—'
}

export function fmtDate(v: unknown): string {
  if (v == null || v === '') return '—'
  const d = dayjs(String(v))
  return d.isValid() ? d.format('YYYY-MM-DD') : '—'
}

/** 秒 -> "24m28s" / "1h05m" / "45s" */
export function seconds(v: unknown): string {
  if (!isNum(v)) return '—'
  const s = Math.max(0, Math.round(v))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60}s`
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`
}

/** ISO 时间 -> "2026-09-05 01:51:33"（本地化） */
export function fmtDateTime(v: string | null | undefined): string {
  if (!v) return '—'
  const d = dayjs(v)
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm:ss') : v
}

/** 策略文件基名（兼容 / 与 \\ 分隔） */
export function strategyName(pathOrName: string): string {
  return (pathOrName || '').split(/[\\/]/).pop() || pathOrName
}

/** 文件大小/市值通用格式 */
export function fmtMoney(v: unknown): string {
  return isNum(v) ? v.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—'
}