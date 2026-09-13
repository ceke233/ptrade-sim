<script setup lang="ts">
// run 详情页：借鉴 supermind 回测详情 —— 页头信息条 + 子 Tab
// （收益概览：指标 + 资金曲线/回撤 + 月度收益；交易明细；输出日志）
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import type { RunDetail } from '@/api/types'
import {
  fmtBig,
  fmtDate,
  fmtNum,
  fmtPct,
  isNum,
  pCls,
  seconds,
  strategyName,
} from '@/lib/format'
import { Badge, type BadgeVariants } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Code2, Loader2, RefreshCw } from '@lucide/vue'
import EChart from '@/components/EChart.vue'
import { drawdownOption, equityOption, monthlyOption } from '@/components/charts'
import type { ECOption } from '@/components/echarts'
import TradesTable from '@/components/TradesTable.vue'
import SourceDialog from '@/components/SourceDialog.vue'

const route = useRoute()
const router = useRouter()

const name = computed(() => String(route.params.name ?? ''))
const sourceOpen = ref(false)
type TabId = 'overview' | 'trades' | 'logs'
const activeTab = ref<TabId>('overview')

const detail = ref<RunDetail | null>(null)
const detailErr = ref('')
let timer: ReturnType<typeof setTimeout> | null = null
let rev = ''
function revOf(d: RunDetail): string {
  const p = (d.progress ?? {}) as Record<string, unknown>
  const s = d.summary
  return [
    d.status, p.phase, p.day_done, p.current_date,
    s ? [s.total_return, s.max_drawdown, s.trade_count].join(',') : '',
    d.series?.dates?.length ?? 0,
  ].join('|')
}
async function loadDetail() {
  try {
    const d = await api.detail(name.value)
    if (revOf(d) !== rev) {
      detail.value = d
      rev = revOf(d)
      detailErr.value = ''
    } else if (detail.value) {
      const prevLogs = detail.value.logs ?? []
      const newLogs = d.logs ?? []
      if (prevLogs.length !== newLogs.length) detail.value = { ...detail.value, logs: newLogs }
    }
  } catch (e) {
    detailErr.value = `详情加载失败：${(e as Error).message}`
  }
}
function startPoll() {
  if (timer) clearTimeout(timer)
  loadDetail()
  timer = setTimeout(startPoll, 3000)
}
function stopPoll() {
  if (timer) clearTimeout(timer)
  timer = null
}
watch(
  () => name.value,
  () => {
    if (!name.value) return
    detail.value = null
    detailErr.value = ''
    rev = ''
    activeTab.value = 'overview'
    stopPoll()
    loadDetail()
    startPoll()
  },
  { immediate: true },
)
onBeforeUnmount(stopPoll)

const summary = computed(() => detail.value?.summary ?? null)
const progress = computed(() => (detail.value?.progress ?? {}) as Record<string, unknown>)
const status = computed(() => detail.value?.status ?? null)

const badgeVar = computed<BadgeVariants['variant']>(() => {
  const map: Record<string, BadgeVariants['variant']> = {
    running: 'destructive', waiting: 'secondary', done: 'default',
    interrupted: 'outline', missing: 'outline',
  }
  return status.value ? (map[status.value] ?? 'outline') : 'outline'
})
const statusLabel: Record<string, string> = {
  running: '运行中', done: '已完成', interrupted: '中断', missing: '数据缺失', waiting: '收尾中',
}

const stateTxt = computed(() => {
  const d = detail.value
  if (!d) return ''
  const p = progress.value
  if (d.status === 'done') return '已完成'
  if (d.status === 'interrupted') return '中断（未完成）'
  const pctOf = p.total_days ? Math.round((((p.day_done as number) || 0) / (p.total_days as number)) * 100) : 0
  return `模拟中 ${String(p.current_date ?? '')} ${p.day_done ?? 0}/${p.total_days ?? '?'} 天（${pctOf}%）`
})

/** 顶部信息条（借鉴 supermind 回测日期|资金|频率|时长） */
const metaLine = computed(() => {
  const p = progress.value
  const cfg = (summary.value?.config ?? {}) as Record<string, unknown>
  const file = strategyName(String(p.strategy ?? cfg.strategy ?? '')) || ''
  const cn = String(cfg.strategy_name ?? '')
  const strategy = cn ? (file && cn !== file ? `${cn}（${file}）` : cn) : file
  const capital = isNum(cfg.capital_base)
    ? (cfg.capital_base as number).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
    : ''
  const start = fmtDate(p.start_date ?? cfg.start_date)
  const end = fmtDate(p.end_date ?? cfg.end_date)
  const freq = '分钟'
  const dur = p.elapsed_sec != null ? ` · 总运行时长 ${seconds(p.elapsed_sec)}` : ''
  return { strategy, capital, start, end, freq, dur }
})

// ---------- 收益概览 ----------
const detailCards = computed(() => {
  const s = (summary.value ?? {}) as Record<string, unknown>
  const ab = detail.value?.alpha_beta
  return [
    { k: '总收益率', v: fmtPct(s.total_return), cls: pCls(s.total_return), sub: '累计' },
    { k: '基准收益', v: fmtPct(s.benchmark_return), cls: pCls(s.benchmark_return), sub: '累计' },
    { k: '年化收益率', v: fmtPct(s.annual_return), cls: pCls(s.annual_return), sub: '策略' },
    { k: '夏普率', v: fmtNum(s.sharpe), cls: '', sub: '' },
    { k: 'Alpha', v: fmtPct(ab?.alpha), cls: pCls(ab?.alpha), sub: '年化' },
    { k: 'Beta', v: fmtNum(ab?.beta), cls: '', sub: '' },
    { k: '最大回撤', v: fmtPct(s.max_drawdown), cls: pCls(s.max_drawdown), sub: '' },
    { k: '卡尔玛', v: fmtNum(s.calmar), cls: '', sub: '' },
    { k: '胜率', v: fmtPct(s.win_rate), cls: '', sub: '' },
    { k: '盈亏比', v: fmtNum(s.profit_loss_ratio), cls: '', sub: '' },
    { k: '期末资产', v: fmtBig(s.final_value), cls: '', sub: '' },
    { k: '成交笔数', v: s.trade_count != null ? String(s.trade_count) : '—', cls: '', sub: '' },
    { k: '累计佣金', v: s.total_commission != null ? fmtBig(s.total_commission) : '—', cls: '', sub: '' },
  ]
})

const monthly = computed<Record<string, number>>(() => {
  const s = summary.value
  return (s?.monthly_returns ?? {}) as Record<string, number>
})
const monthlyRows = computed(() => {
  const m = monthly.value
  const stats = summary.value?.monthly_stats
  const ext = detail.value?.monthly_ext ?? {}
  const best = stats?.best_month
  const worst = stats?.worst_month
  // 逐月复利累计：cum_t = (1+cum_{t-1}) * (1+r_t) - 1
  // 月收益用 daily_stats 聚合口径（ext.ret，同基准/超额起点，表格自洽）
  let cumFactor = 1
  const arr = Object.entries(m)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([month, val]) => {
      const e = ext[month]
      const r = e?.ret ?? val ?? 0
      cumFactor *= 1 + r
      return {
        month,
        val: r * 100,
        cum: (cumFactor - 1) * 100,
        bench: e?.bench != null ? e.bench * 100 : null,
        excess: e?.excess != null ? e.excess * 100 : null,
        mdd: e?.mdd != null ? e.mdd * 100 : null,
        trades: e?.trades ?? null,
        commission: e?.commission ?? null,
        beta: e?.beta ?? null,
        alpha: e?.alpha ?? null,
        isBest: best?.month === month,
        isWorst: worst?.month === month,
      }
    })
  return { arr, stats }
})
const monthlyStatCards = computed(() => {
  const st = summary.value?.monthly_stats
  if (!st) return []
  return [
    { k: '月胜率', v: fmtPct(st.win_rate), cls: '' },
    { k: '月均收益', v: fmtPct(st.mean), cls: pCls(st.mean) },
    { k: '月收益中位', v: fmtPct(st.median), cls: pCls(st.median) },
    { k: '月波动', v: fmtPct(st.std), cls: '' },
    { k: '最佳月', v: st.best_month ? `${st.best_month.month ?? ''} ${fmtPct(st.best_month.return)}` : '—', cls: pCls(st.best_month?.return) },
    { k: '最差月', v: st.worst_month ? `${st.worst_month.month ?? ''} ${fmtPct(st.worst_month.return)}` : '—', cls: pCls(st.worst_month?.return) },
  ]
})

const equityOpt = computed<ECOption | null>(() => (detail.value ? equityOption(detail.value.series) : null))
const ddOpt = computed<ECOption | null>(() => (detail.value ? drawdownOption(detail.value.series) : null))
const monthOpt = computed<ECOption | null>(() => (Object.keys(monthly.value).length ? monthlyOption(monthly.value) : null))

const tabs: { id: TabId; label: string }[] = [
  { id: 'overview', label: '收益概览' },
  { id: 'trades', label: '交易明细' },
  { id: 'logs', label: '输出日志' },
]

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push({ name: 'runs' })
}

// ---------- 输出日志（独立分页加载，不依赖 detail.logs 的 60 行） ----------
const LOG_PAGE = 500
const logLines = ref<string[]>([])
const logTotal = ref(0)
const logLoading = ref(false)
const logLoadingMore = ref(false)
const logErr = ref('')

async function loadLogs() {
  // 首次/刷新：加载尾部 LOG_PAGE 行；不清空已有内容（新版日志自动追加）
  if (logLoading.value) return
  logLoading.value = true
  logErr.value = ''
  try {
    const d = await api.log(name.value, LOG_PAGE)
    logLines.value = d.lines ?? []
    logTotal.value = d.total ?? logLines.value.length
  } catch (e) {
    logErr.value = `日志加载失败：${(e as Error).message}`
  } finally {
    logLoading.value = false
  }
}

async function loadEarlier() {
  // 向前翻页：offset=当前已加载行数，拉到更早的 LOG_PAGE 行，拼在现有内容前面
  if (logLoadingMore.value || logLoading.value) return
  logLoadingMore.value = true
  logErr.value = ''
  try {
    const d = await api.log(name.value, LOG_PAGE, logLines.value.length)
    if (d.lines?.length) logLines.value = [...d.lines, ...logLines.value]
    logTotal.value = d.total ?? logTotal.value
  } catch (e) {
    logErr.value = `加载更早日志失败：${(e as Error).message}`
  } finally {
    logLoadingMore.value = false
  }
}

const showLoadEarlier = computed(
  () => logLines.value.length < logTotal.value,
)

// 首次进入日志 Tab 时加载尾部；run 切换时清空并重置
watch(
  () => [name.value, activeTab.value] as const,
  ([n, tab], [prevN]) => {
    if (!n) return
    if (n !== prevN) {
      logLines.value = []
      logTotal.value = 0
      logErr.value = ''
    }
    if (tab === 'logs') loadLogs()
  },
)
</script>

<template>
  <main class="mx-auto max-w-7xl px-3 py-3 sm:px-4 sm:py-4">
    <!-- 页头：返回 + 名称 + 状态 + 源码 -->
    <div class="flex flex-wrap items-center gap-2 sm:gap-3">
      <Button variant="outline" size="sm" class="shrink-0 gap-1.5 px-2" @click="goBack">
        ← 返回
      </Button>
      <h2 class="min-w-0 flex-1 truncate font-mono text-sm font-semibold sm:text-lg">{{ name }}</h2>
      <Badge v-if="status" :variant="badgeVar" class="shrink-0 px-1.5 py-0 text-[10px] sm:px-2.5 sm:text-xs">
        {{ statusLabel[status] ?? status }}
      </Badge>
      <span v-if="detail?.status === 'running'" class="flex items-center gap-1 text-xs text-destructive">
        <Loader2 class="size-3 animate-spin" /> 实时更新
      </span>
      <Button
        v-if="status === 'done'"
        variant="outline"
        size="sm"
        class="shrink-0 gap-1.5 px-2"
        @click="sourceOpen = true"
      >
        <Code2 class="size-4" />
        源码
      </Button>
    </div>

    <!-- 信息条（supermind 式：日期 | 资金 | 频率 | 时长） -->
    <div v-if="detail" class="mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-muted-foreground">
      <template v-if="metaLine.strategy">
        <span class="font-medium text-foreground/80">策略 {{ metaLine.strategy }}</span>
        <span class="text-border">|</span>
      </template>
      <span>回测日期 {{ metaLine.start }} 至 {{ metaLine.end }}</span>
      <span class="text-border">|</span>
      <span>资金 {{ metaLine.capital || '—' }}</span>
      <span class="text-border">|</span>
      <span>频率 {{ metaLine.freq }}</span>
      <span v-if="metaLine.dur">{{ metaLine.dur }}</span>
    </div>

    <!-- 错误 / 加载 -->
    <div v-if="detailErr" class="mt-4 rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
      {{ detailErr }}
    </div>
    <div v-else-if="!detail" class="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
      <Loader2 class="size-4 animate-spin" />
      加载详情…
    </div>

    <template v-else>
      <!-- 子 Tab -->
      <div class="mt-4 flex items-center gap-1 border-b">
        <button
          v-for="t in tabs"
          :key="t.id"
          type="button"
          class="border-b-2 px-3 py-2 text-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
          :class="
            activeTab === t.id
              ? 'border-foreground font-medium text-foreground'
              : 'border-transparent text-muted-foreground hover:border-muted hover:text-foreground/80'
          "
          @click="activeTab = t.id"
        >
          {{ t.label }}
        </button>
      </div>

      <!-- Tab: 收益概览 -->
      <div v-if="activeTab === 'overview'" class="pt-4">
        <div class="mb-2 text-xs text-muted-foreground">
          {{ stateTxt }} · 已耗时 {{ seconds(progress.elapsed_sec as number | undefined) }}
          <span v-if="detail.status === 'running'" class="text-destructive">· 数据实时更新</span>
        </div>

        <!-- 指标网格（supermind 式成对核心指标，放大整页密度） -->
        <div class="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6">
          <Card v-for="c in detailCards" :key="c.k" class="px-2 py-2 sm:px-3 sm:py-2.5">
            <div class="flex items-baseline justify-between gap-1">
              <span class="text-[10px] text-muted-foreground sm:text-[11px]">{{ c.k }}</span>
              <span v-if="c.sub" class="text-[10px] text-muted-foreground/60">{{ c.sub }}</span>
            </div>
            <div class="mt-0.5 truncate text-sm font-semibold sm:text-base" :class="c.cls">{{ c.v }}</div>
          </Card>
        </div>

        <!-- 资金曲线大图 -->
        <Card class="mt-4 px-2 py-3">
          <div class="mb-1 px-2 text-xs font-medium text-muted-foreground">资金曲线 · 累计收益率</div>
          <div class="h-64 w-full sm:h-[24rem]">
            <EChart v-if="equityOpt" :option="equityOpt" class="h-full w-full" />
          </div>
        </Card>

        <!-- 回撤图 -->
        <Card class="mt-3 px-2 py-3">
          <div class="mb-1 px-2 text-xs font-medium text-muted-foreground">回撤</div>
          <div class="h-40 w-full sm:h-44">
            <EChart v-if="ddOpt" :option="ddOpt" class="h-full w-full" />
          </div>
        </Card>

        <!-- 月度收益 -->
        <template v-if="monthOpt || monthlyRows.stats">
          <Card class="mt-3 px-2 py-3">
            <div class="mb-1 px-2 text-xs font-medium text-muted-foreground">月度收益分布</div>
            <div class="h-48 w-full sm:h-52">
              <EChart v-if="monthOpt" :option="monthOpt" class="h-full w-full" />
            </div>
            <div v-if="monthlyStatCards.length" class="mt-2 grid grid-cols-2 gap-2 border-t px-2 pt-3 sm:grid-cols-3 md:grid-cols-6">
              <div v-for="c in monthlyStatCards" :key="c.k">
                <div class="text-[10px] text-muted-foreground">{{ c.k }}</div>
                <div class="text-sm font-semibold" :class="c.cls">{{ c.v }}</div>
              </div>
            </div>
          </Card>
          <!-- 月度明细表（窄屏横向滚动） -->
          <Card v-if="monthlyRows.arr.length" class="mt-3 px-2 py-3">
            <div class="mb-2 px-2 text-xs font-medium text-muted-foreground">月度收益明细</div>
            <div class="max-h-64 overflow-auto px-2">
              <table class="w-full border-collapse text-xs">
                <thead class="sticky top-0 z-10 bg-background">
                  <tr class="border-b text-left text-muted-foreground">
                    <th class="py-1.5 pr-3 font-medium">月份</th>
                    <th class="py-1.5 pr-3 text-right font-medium">策略</th>
                    <th class="py-1.5 pr-3 text-right font-medium">基准</th>
                    <th class="py-1.5 pr-3 text-right font-medium">超额</th>
                    <th class="py-1.5 pr-3 text-right font-medium">Beta</th>
                    <th class="py-1.5 pr-3 text-right font-medium">Alpha</th>
                    <th class="py-1.5 pr-3 text-right font-medium">累计</th>
                    <th class="py-1.5 pr-3 text-right font-medium">月回撤</th>
                    <th class="py-1.5 pr-3 text-right font-medium">笔数</th>
                    <th class="py-1.5 pr-3 text-right font-medium">佣金</th>
                    <th class="py-1.5 text-right font-medium">标记</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="r in monthlyRows.arr" :key="r.month" class="border-b last:border-0">
                    <td class="whitespace-nowrap py-1.5 pr-3 font-mono">{{ r.month }}</td>
                    <td class="py-1.5 pr-3 text-right font-mono" :class="pCls(r.val / 100)">{{ (r.val >= 0 ? '+' : '') + r.val.toFixed(2) + '%' }}</td>
                    <td class="py-1.5 pr-3 text-right font-mono" :class="pCls(r.bench != null ? r.bench / 100 : null)">{{ r.bench != null ? (r.bench >= 0 ? '+' : '') + r.bench.toFixed(2) + '%' : '—' }}</td>
                    <td class="py-1.5 pr-3 text-right font-mono" :class="pCls(r.excess != null ? r.excess / 100 : null)">{{ r.excess != null ? (r.excess >= 0 ? '+' : '') + r.excess.toFixed(2) + '%' : '—' }}</td>
                    <td class="py-1.5 pr-3 text-right font-mono">{{ r.beta != null ? r.beta.toFixed(2) : '—' }}</td>
                    <td class="py-1.5 pr-3 text-right font-mono" :class="pCls(r.alpha)">{{ r.alpha != null ? (r.alpha >= 0 ? '+' : '') + (r.alpha * 100).toFixed(1) + '%' : '—' }}</td>
                    <td class="py-1.5 pr-3 text-right font-mono" :class="pCls(r.cum / 100)">{{ (r.cum >= 0 ? '+' : '') + r.cum.toFixed(2) + '%' }}</td>
                    <td class="py-1.5 pr-3 text-right font-mono" :class="pCls(r.mdd != null ? r.mdd / 100 : null)">{{ r.mdd != null ? r.mdd.toFixed(2) + '%' : '—' }}</td>
                    <td class="whitespace-nowrap py-1.5 pr-3 text-right font-mono">{{ r.trades ?? '—' }}</td>
                    <td class="whitespace-nowrap py-1.5 pr-3 text-right font-mono">{{ r.commission != null ? r.commission.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) : '—' }}</td>
                    <td class="whitespace-nowrap py-1.5 text-right">
                      <Badge v-if="r.isBest" variant="outline" class="text-destructive border-destructive/40 bg-destructive/10">最佳</Badge>
                      <Badge v-else-if="r.isWorst" variant="outline" class="text-emerald-600 border-emerald-500/40 bg-emerald-500/10">最差</Badge>
                      <span v-else class="text-muted-foreground">—</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </Card>
        </template>
      </div>

      <!-- Tab: 交易明细 -->
      <div v-if="activeTab === 'trades'" class="pt-4">
        <TradesTable :name="name" />
      </div>

      <!-- Tab: 输出日志 -->
      <div v-if="activeTab === 'logs'" class="pt-4">
        <div class="mb-2 flex items-center justify-between text-xs text-muted-foreground">
          <span>
            共 {{ logTotal }} 行 · 已加载 {{ logLines.length }} 行
            <span v-if="logLoading"><Loader2 class="ml-1 inline size-3 animate-spin" /></span>
          </span>
          <Button variant="outline" size="xs" class="gap-1" :disabled="logLoading" @click="loadLogs()">
            <RefreshCw class="size-3" /> 刷新
          </Button>
        </div>
        <div v-if="logErr" class="mb-2 text-xs text-destructive">{{ logErr }}</div>
        <pre class="max-h-[36rem] overflow-auto whitespace-pre-wrap rounded-md border bg-slate-950 p-3 font-mono text-xs leading-relaxed text-slate-100">{{ logLines.join('\n') || '（暂无日志）' }}</pre>
        <div class="mt-2 flex justify-center">
          <Button
            v-if="showLoadEarlier"
            variant="outline"
            size="xs"
            class="gap-1"
            :disabled="logLoadingMore || logLoading"
            @click="loadEarlier()"
          >
            <Loader2 v-if="logLoadingMore" class="size-3 animate-spin" />
            加载更早日志（还剩 {{ logTotal - logLines.length }} 行）
          </Button>
          <span v-else-if="logTotal > 0" class="text-xs text-muted-foreground">已到日志开头</span>
        </div>
      </div>
    </template>

    <SourceDialog :name="sourceOpen ? name : null" @close="sourceOpen = false" />
  </main>
</template>