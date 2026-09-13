<script setup lang="ts">
// 交易明细表（supermind 风格）：服务端分页 + 中文表头 + 买卖徽章
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { TradesRow } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { ChevronLeft, ChevronRight, Loader2 } from '@lucide/vue'
import { fmtDateTime } from '@/lib/format'

const props = defineProps<{ name: string }>()

const rows = ref<TradesRow[]>([])
const total = ref(0)
const error = ref('')
const loading = ref(false)
const page = ref(1)
const PAGE_SIZE = 20

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const pageRows = computed(() => rows.value)

const sideMeta: Record<string, { label: string; cls: string }> = {
  buy: { label: '买入', cls: 'text-destructive border-destructive/40 bg-destructive/10' },
  sell: { label: '卖出', cls: 'text-emerald-600 border-emerald-500/40 bg-emerald-500/10' },
}

function num(v: unknown): number | null {
  if (typeof v === 'number' && !Number.isNaN(v)) return v
  if (typeof v === 'string' && v !== '') {
    const n = Number(v)
    return Number.isNaN(n) ? null : n
  }
  return null
}
function money(v: unknown): string {
  const n = num(v)
  return n == null ? '—' : Math.abs(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
function price(v: unknown): string {
  const n = num(v)
  return n == null ? '—' : n.toFixed(3)
}
function sideOf(r: TradesRow): 'buy' | 'sell' | '' {
  const s = String(r.side ?? '').toLowerCase()
  return s === 'buy' ? 'buy' : s === 'sell' ? 'sell' : ''
}
/** 成交方向：买入正量 / 卖出负量（与 supermind 数量列一致） */
function signedAmount(r: TradesRow): number | null {
  const n = num(r.amount)
  if (n == null) return null
  return sideOf(r) === 'sell' ? -Math.abs(n) : Math.abs(n)
}
function pnlCls(v: unknown): string {
  const n = num(v)
  if (n == null) return ''
  return n >= 0 ? 'text-destructive' : 'text-emerald-600'
}

async function load() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    const d = await api.trades(props.name, page.value, PAGE_SIZE)
    rows.value = (d.rows ?? []) as TradesRow[]
    total.value = d.total ?? rows.value.length
    if (d.error) error.value = d.error
  } catch (e) {
    error.value = `交易明细加载失败：${(e as Error).message}`
  } finally {
    loading.value = false
  }
}

// 翻页重新请求；run 变化回到第 1 页
watch(
  () => props.name,
  () => {
    page.value = 1
    load()
  },
)
watch(page, load)
onMounted(load)
</script>

<template>
  <div>
    <div v-if="error" class="mb-2 text-xs text-destructive">{{ error }}</div>

    <div class="flex items-center justify-between text-xs text-muted-foreground">
      <span>
        共 {{ total }} 笔
        <span v-if="loading"><Loader2 class="ml-1 inline size-3 animate-spin" /></span>
      </span>
      <span v-if="total > PAGE_SIZE">
        第 {{ page }} / {{ totalPages }} 页
      </span>
    </div>

    <div v-if="!rows.length && !error" class="py-16 text-center text-sm text-muted-foreground">
      <Loader2 v-if="loading" class="mx-auto mb-2 size-4 animate-spin" />
      {{ loading ? '加载中…' : '暂无交易记录' }}
    </div>

    <template v-else>
      <div class="mt-2 overflow-x-auto rounded-md border">
        <Table>
          <TableHeader>
            <TableRow class="bg-muted/50 hover:bg-muted/50">
              <TableHead>时间</TableHead>
              <TableHead>代码</TableHead>
              <TableHead>方向</TableHead>
              <TableHead class="text-right">成交价</TableHead>
              <TableHead class="text-right">数量</TableHead>
              <TableHead class="text-right">成交额</TableHead>
              <TableHead class="text-right">手续费</TableHead>
              <TableHead class="text-right">平仓盈亏</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow v-for="(r, i) in pageRows" :key="String(r.order_id ?? r.time ?? i)">
              <TableCell class="whitespace-nowrap font-mono text-xs">{{ fmtDateTime(r.time) }}</TableCell>
              <TableCell class="font-mono text-xs">{{ r.security ?? '—' }}</TableCell>
              <TableCell>
                <template v-if="sideOf(r)">
                  <Badge variant="outline" :class="sideMeta[sideOf(r) as 'buy' | 'sell'].cls">
                    {{ sideMeta[sideOf(r) as 'buy' | 'sell'].label }}
                  </Badge>
                </template>
                <span v-else class="text-xs text-muted-foreground">—</span>
              </TableCell>
              <TableCell class="text-right font-mono text-xs">{{ price(r.price) }}</TableCell>
              <TableCell class="text-right font-mono text-xs">{{ signedAmount(r)?.toLocaleString('zh-CN') ?? '—' }}</TableCell>
              <TableCell class="text-right font-mono text-xs">{{ money(r.turnover) }}</TableCell>
              <TableCell class="text-right font-mono text-xs">{{ money(r.commission) }}</TableCell>
              <TableCell class="text-right font-mono text-xs" :class="pnlCls(r.trade_pnl)">
                {{ num(r.trade_pnl) != null ? (num(r.trade_pnl)! >= 0 ? '+' : '') + money(r.trade_pnl) : '—' }}
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </div>

      <div v-if="totalPages > 1" class="mt-3 flex flex-wrap items-center justify-end gap-2 text-xs">
        <Button variant="outline" size="xs" :disabled="page <= 1" @click="page--">
          <ChevronLeft class="size-3" /> 上一页
        </Button>
        <span class="text-muted-foreground">{{ page }} / {{ totalPages }}</span>
        <Button variant="outline" size="xs" :disabled="page >= totalPages" @click="page++">
          下一页 <ChevronRight class="size-3" />
        </Button>
      </div>
    </template>
  </div>
</template>