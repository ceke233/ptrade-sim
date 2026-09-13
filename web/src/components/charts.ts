// 由 SeriesData 构造 ECharts option：资金曲线(累计收益率%) + 回撤
import type { ECOption } from './echarts'
import type { SeriesData } from '../api/types'

/** 归一化为累计收益率 %：v / base - 1 */
function normSeries(vals: (number | null)[]): (number | null)[] {
  if (!vals || vals.length < 2) return []
  const base = vals[0]
  if (!base) return vals.map(() => null)
  return vals.map((v) => (v == null ? null : ((v as number) / base - 1) * 100))
}

/** 深色模式感知的图表调色板（浅色/深色各一套） */
function palette() {
  const dark = typeof document !== 'undefined' && document.documentElement.classList.contains('dark')
  return dark
    ? {
        axisLabel: '#94a3b8', // slate-400：深底上足够亮
        axisLine: '#334155', // slate-700
        splitLine: '#1e293b', // slate-800
        legend: '#cbd5e1', // slate-300
        strategy: '#ef4444', // 亮红
        bench: '#3b82f6', // 亮蓝
        drawdown: '#f59e0b', // 亮橙
      }
    : {
        axisLabel: '#64748b',
        axisLine: '#cbd5e1',
        splitLine: '#eef1f5',
        legend: '#475569',
        strategy: '#c0392b',
        bench: '#2c6fbb',
        drawdown: '#e67e22',
      }
}

export function equityOption(s: SeriesData): ECOption {
  const dates = s.dates ?? []
  const names: string[] = ['策略']
  const pal = palette()
  const series = [
    {
      name: '策略',
      type: 'line' as const,
      data: normSeries(s.equity ?? []),
      showSymbol: false,
      lineStyle: { width: 1.6, color: pal.strategy },
      itemStyle: { color: pal.strategy },
      connectNulls: false,
    },
  ]
  if ((s.bench ?? []).length > 1) {
    series.push({
      name: '基准',
      type: 'line' as const,
      data: normSeries(s.bench ?? []),
      showSymbol: false,
      lineStyle: { width: 1.2, color: pal.bench },
      itemStyle: { color: pal.bench },
      connectNulls: false,
    })
    names.push('基准')
  }
  return {
    animation: false,
    tooltip: { trigger: 'axis' },
    legend: { data: names, top: 0, textStyle: { color: pal.legend } },
    grid: { left: 58, right: 20, top: 32, bottom: 30 },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: false,
      axisLabel: { color: pal.axisLabel },
      axisLine: { lineStyle: { color: pal.axisLine } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: pal.axisLabel, formatter: '{value}%' },
      splitLine: { lineStyle: { color: pal.splitLine } },
    },
    series,
  }
}

export function drawdownOption(s: SeriesData): ECOption {
  const dates = s.dates ?? []
  const vals = (s.drawdown ?? []).map((v) =>
    v == null ? null : (v as number) * 100,
  )
  const pal = palette()
  return {
    animation: false,
    tooltip: { trigger: 'axis' },
    grid: { left: 58, right: 20, top: 20, bottom: 30 },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: false,
      axisLabel: { color: pal.axisLabel },
      axisLine: { lineStyle: { color: pal.axisLine } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: pal.axisLabel, formatter: '{value}%' },
      splitLine: { lineStyle: { color: pal.splitLine } },
    },
    series: [
      {
        name: '回撤',
        type: 'line',
        data: vals,
        showSymbol: false,
        lineStyle: { width: 1.2, color: pal.drawdown },
        itemStyle: { color: pal.drawdown },
        areaStyle: { color: `${pal.drawdown}33` }, // 20% 透明度
        connectNulls: false,
      },
    ],
  }
}

/** 月度收益柱状图：正红负绿（A股配色，与 pCls 一致） */
export function monthlyOption(monthly: Record<string, number>): ECOption {
  const months = Object.keys(monthly).sort()
  const vals = months.map((m) => (monthly[m] ?? 0) * 100)
  const pal = palette()
  const red = '#ef4444'
  const green = '#22c55e'
  const itemColors = vals.map((v) => (v >= 0 ? red : green))
  return {
    animation: false,
    tooltip: {
      trigger: 'axis',
      formatter: (ps: unknown) => {
        const arr = ps as { axisValue: string; value: number }[]
        const p = arr[0]
        return `${p.axisValue}<br/>${p.value >= 0 ? '+' : ''}${p.value.toFixed(2)}%`
      },
    },
    grid: { left: 58, right: 20, top: 24, bottom: 34 },
    xAxis: {
      type: 'category',
      data: months,
      axisLabel: { color: pal.axisLabel, fontSize: 10 },
      axisLine: { lineStyle: { color: pal.axisLine } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: pal.axisLabel, formatter: '{value}%' },
      splitLine: { lineStyle: { color: pal.splitLine } },
    },
    series: [
      {
        name: '月度收益',
        type: 'bar',
        data: vals,
        itemStyle: {
          color: (p: { dataIndex: number }) => itemColors[p.dataIndex] ?? red,
          borderRadius: [2, 2, 0, 0],
        },
      },
    ],
  }
}
