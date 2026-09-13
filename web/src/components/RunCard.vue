<script setup lang="ts">
// 单个 run 卡片：状态 + 时间 + 指标 + 进度；点击卡片整卡进入详情页（不再就地展开）
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import type { RunInfo } from '@/api/types'
import {
  fmtDate,
  fmtDateTime,
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
import { Progress } from '@/components/ui/progress'
import { ChevronRight, Code2, Loader2 } from '@lucide/vue'

const props = defineProps<{
  run: RunInfo
}>()
const emit = defineEmits<{ (e: 'source', name: string): void }>()

const router = useRouter()

const badgeVar = computed<BadgeVariants['variant']>(() => {
  const map: Record<string, BadgeVariants['variant']> = {
    running: 'destructive',
    waiting: 'secondary',
    done: 'default',
    interrupted: 'outline',
    missing: 'outline',
  }
  return map[props.run.status] ?? 'outline'
})
const statusLabel: Record<string, string> = {
  running: '运行中', done: '已完成', interrupted: '中断', missing: '数据缺失', waiting: '收尾中',
}

const pct = computed(() =>
  props.run.total_days ? Math.min(100, ((props.run.day_done ?? 0) / props.run.total_days) * 100) : 0,
)
const eta = computed(() => {
  const r = props.run
  if (r.status === 'running' && isNum(r.elapsed_sec) && pct.value > 2 && pct.value < 99)
    return ` ｜ ETA ${seconds((r.elapsed_sec! / pct.value) * (100 - pct.value))}`
  return ''
})
const timeline = computed(() => {
  const r = props.run
  let dur = '—'
  if (r.status === 'done' && r.done_at && r.started_at)
    dur = seconds((new Date(r.done_at).getTime() - new Date(r.started_at).getTime()) / 1000)
  else if (isNum(r.elapsed_sec)) dur = seconds(r.elapsed_sec)
  const done = r.done_at ? ` · 完成 ${fmtDateTime(r.done_at)}` : ''
  return `${fmtDateTime(r.started_at)} · 历时 ${dur}${done}`
})

const m = computed(() => props.run.metrics)
const cardMetrics = computed(() => [
  { k: '总收益率', v: fmtPct(m.value?.total_return, 1), cls: pCls(m.value?.total_return) },
  { k: '年化', v: fmtPct(m.value?.annual_return, 1), cls: pCls(m.value?.annual_return) },
  { k: '夏普', v: fmtNum(m.value?.sharpe), cls: '' },
  { k: '最大回撤', v: fmtPct(m.value?.max_drawdown, 1), cls: pCls(m.value?.max_drawdown) },
])

const isActive = computed(() => props.run.status === 'running' || props.run.status === 'waiting')

/** 基准代码 -> 中文名（未知保留原代码） */
const BENCH_CN: Record<string, string> = {
  '000300.SS': '沪深300',
  '000905.SS': '中证500',
  '000016.SS': '上证50',
  '000852.SS': '中证1000',
  '399006.SZ': '创业板指',
  '000688.SS': '科创50',
}
function benchLabel(code: string | null | undefined): string {
  if (!code) return '—'
  return BENCH_CN[code] ? `${code} ${BENCH_CN[code]}` : code
}
</script>

<template>
  <Card class="overflow-hidden transition-shadow hover:shadow-md">
    <!-- 点击整卡进入详情页（Ctrl/Cmd+点击或中键新标签打开） -->
    <div
      class="flex cursor-pointer flex-wrap items-center gap-x-4 gap-y-2 px-3 py-2.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:px-4 sm:py-3"
      role="link"
      tabindex="0"
      @click="router.push({ name: 'run-detail', params: { name: props.run.name } })"
      @keydown.enter.space="router.push({ name: 'run-detail', params: { name: props.run.name } })"
    >
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span class="truncate font-mono text-xs font-semibold sm:text-sm">{{ props.run.name }}</span>
          <Badge :variant="badgeVar" class="shrink-0 px-1.5 py-0 text-[10px] sm:px-2.5 sm:text-xs">
            {{ statusLabel[props.run.status] ?? props.run.status }}
          </Badge>
          <span v-if="isActive">
            <Loader2 class="size-3.5 animate-spin text-destructive" />
          </span>
        </div>

        <!-- 策略中文名（来自 summary.strategy_name；无则退回文件名） -->
        <div class="mt-0.5 truncate text-sm font-medium text-foreground">
          {{ props.run.strategy_name || strategyName(props.run.strategy || props.run.name) }}
        </div>

        <!-- 基准 + 区间（多行小字带标签） -->
        <div class="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
          <span class="inline-flex items-center gap-1">
            <span class="text-[10px] text-muted-foreground/70">基准</span>
            <span class="font-mono">{{ benchLabel(props.run.benchmark) }}</span>
          </span>
          <span class="inline-flex items-center gap-1">
            <span class="text-[10px] text-muted-foreground/70">开始</span>
            <span class="font-mono">{{ fmtDate(props.run.start_date) }}</span>
          </span>
          <span class="inline-flex items-center gap-1">
            <span class="text-[10px] text-muted-foreground/70">结束</span>
            <span class="font-mono">{{ fmtDate(props.run.end_date) }}</span>
          </span>
        </div>

        <div class="truncate text-[11px] text-muted-foreground/70">{{ timeline }}</div>
      </div>

      <!-- 4 个速览指标：桌面右侧一行；移动端下方 2 列网格 -->
      <div class="hidden items-center gap-4 sm:flex">
        <div v-for="c in cardMetrics" :key="c.k" class="text-right">
          <div class="text-[10px] text-muted-foreground">{{ c.k }}</div>
          <div class="text-sm font-semibold" :class="c.cls">{{ c.v }}</div>
        </div>
      </div>

      <!-- 进度：桌面定宽；移动端占满整行 -->
      <div class="w-full min-w-0 sm:w-36 sm:flex-1">
        <Progress
          :model-value="pct"
          :class="props.run.status === 'done' ? '[&>div]:bg-primary' : '[&>div]:bg-destructive'"
        />
        <div class="mt-1 truncate whitespace-nowrap text-[11px] text-muted-foreground">
          {{ props.run.total_days != null ? `${props.run.day_done ?? 0}/${props.run.total_days} 天` : '—' }}
          {{ fmtDate(props.run.current_date) }}{{ eta }}
        </div>
      </div>

      <div class="flex flex-col items-end gap-1.5">
        <Button
          v-if="props.run.status === 'done' && props.run.source_exists"
          variant="outline"
          size="xs"
          class="gap-1"
          @click.stop="emit('source', props.run.name)"
        >
          <Code2 class="size-3.5" />
          源码
        </Button>
        <ChevronRight class="size-4 text-muted-foreground/60 transition-transform group-hover:translate-x-0.5" />
      </div>
    </div>

    <!-- 移动端指标网格（<sm 时显示，替代右侧隐藏的速览指标） -->
    <div class="grid grid-cols-2 gap-x-3 gap-y-1 border-t px-3 py-2 sm:hidden">
      <div v-for="c in cardMetrics" :key="'m-' + c.k" class="flex items-baseline justify-between gap-2">
        <span class="text-[11px] text-muted-foreground">{{ c.k }}</span>
        <span class="text-sm font-semibold" :class="c.cls">{{ c.v }}</span>
      </div>
    </div>
  </Card>
</template>