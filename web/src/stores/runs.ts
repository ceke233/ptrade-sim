import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api } from '@/api/client'
import type { RunInfo } from '@/api/types'

/** 看板统一轮询间隔（列表/详情一致，3s） */
export const REFRESH_MS = 3000

/** 看板主 store：run 列表 + 轮询节奏 + 交互态 */
export const useRunsStore = defineStore('runs', () => {
  const runs = ref<RunInfo[]>([])
  const error = ref('')
  const sourceName = ref<string | null>(null)
  const lastUpdatedAt = ref<number>(0)
  let timer: ReturnType<typeof setTimeout> | null = null
  let busy = false

  const isActive = (r: RunInfo) => r.status === 'running' || r.status === 'waiting'
  const runningCount = computed(() => runs.value.filter(isActive).length)
  const doneCount = computed(() => runs.value.filter((r) => r.status === 'done').length)
  const interruptedCount = computed(() =>
    runs.value.filter((r) => r.status === 'interrupted').length,
  )
  const anyRunning = computed(() => runningCount.value > 0)
  const intervalMs = computed(() => REFRESH_MS)

  async function refresh() {
    if (busy) return
    busy = true
    try {
      const data = await api.runs()
      runs.value = data.runs ?? []
      lastUpdatedAt.value = Date.now()
      error.value = ''
    } catch (e) {
      error.value = (e as Error).message
    } finally {
      busy = false
    }
  }

  function schedule() {
    if (timer) clearTimeout(timer)
    timer = setTimeout(async () => {
      await refresh()
      schedule()
    }, intervalMs.value)
  }

  function start() {
    refresh()
    schedule()
  }
  function stop() {
    if (timer) clearTimeout(timer)
    timer = null
  }

  return {
    runs,
    error,
    sourceName,
    lastUpdatedAt,
    runningCount,
    doneCount,
    interruptedCount,
    anyRunning,
    intervalMs,
    refresh,
    start,
    stop,
  }
})
