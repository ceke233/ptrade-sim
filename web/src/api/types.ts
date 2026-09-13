// 后端 /api 返回类型定义（与 ptrade-sim/server.py 对应）

export type RunStatus = 'running' | 'done' | 'interrupted' | 'missing' | 'waiting'

export interface SlimMetrics {
  total_return: number | null
  annual_return: number | null
  sharpe: number | null
  max_drawdown: number | null
  calmar: number | null
  final_value: number | null
  benchmark_return: number | null
  trade_count: number | null
  total_commission: number | null
}

export interface RunInfo {
  name: string
  status: RunStatus
  phase: string | null
  strategy: string | null
  strategy_name: string | null
  start_date: string | null
  end_date: string | null
  capital_base: number | null
  benchmark: string | null
  total_days: number | null
  day_done: number | null
  current_date: string | null
  started_at: string | null
  elapsed_sec: number | null
  done_at: string | null
  metrics: SlimMetrics | null
  source_path: string | null
  source_exists: boolean
}

export interface FullSummary {
  total_return?: number
  annual_return?: number
  sharpe?: number
  max_drawdown?: number
  calmar?: number
  win_rate?: number
  profit_loss_ratio?: number
  final_value?: number
  benchmark_return?: number
  trade_count?: number
  total_commission?: number
  // 月度分布：{ "2024-01": 0.0696 }（比例）
  monthly_returns?: Record<string, number>
  monthly_stats?: {
    win_rate?: number
    best_month?: { month?: string; return?: number }
    worst_month?: { month?: string; return?: number }
    mean?: number
    median?: number
    std?: number
    skew?: number
    kurt?: number
  }
  config?: {
    strategy?: string
    strategy_name?: string
    start_date?: string
    end_date?: string
    capital_base?: number
    benchmark?: string
  }
  // 其余字段可忽略
  [k: string]: unknown
}

export interface SeriesData {
  dates: string[]
  equity: (number | null)[]
  drawdown: (number | null)[]
  bench: (number | null)[]
}

export interface RunDetail {
  name: string
  status: RunStatus
  progress: Record<string, unknown> | null
  summary: FullSummary | null
  series: SeriesData
  /** 按月聚合扩展：{ "2024-01": {ret, bench, excess, mdd, trades, commission, beta, alpha} }（比例） */
  monthly_ext?: Record<
    string,
    {
      ret?: number
      bench?: number | null
      excess?: number | null
      mdd?: number | null
      trades?: number
      commission?: number
      beta?: number | null
      alpha?: number | null
    }
  >
  /** 整段回测 Alpha（年化）/ Beta（日收益回归） */
  alpha_beta?: { beta: number | null; alpha: number | null }
  logs: string[]
}

export interface TradesResp {
  rows: Record<string, unknown>[]
  total?: number
  error?: string
}

/** trades.csv 行（后端 astype(object) 全字段字符串/数字混合） */
export interface TradesRow {
  time?: string
  security?: string
  side?: string // 'buy' | 'sell'
  amount?: number | string
  price?: number | string
  turnover?: number | string
  commission?: number | string
  order_id?: string
  trade_pnl?: number | string
  [k: string]: unknown
}

export interface SourceResp {
  path: string | null
  exists: boolean
  source: string | null
}

export interface RunsResp {
  runs: RunInfo[]
}
