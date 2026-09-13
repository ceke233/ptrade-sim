<script setup lang="ts">
// 列表首页：统计条 + run 卡片列表（点击卡片进入详情页，不展开）
import { useRunsStore } from '@/stores/runs'
import RunCard from '@/components/RunCard.vue'

const store = useRunsStore()
</script>

<template>
  <main class="mx-auto max-w-6xl px-4 py-4">
    <div class="mb-3 flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <span>
        共 {{ store.runs.length }} 个 run ｜ 完成 {{ store.doneCount }} ｜
        运行中 {{ store.runningCount }} ｜ 中断 {{ store.interruptedCount }}
        <span v-if="store.error" class="ml-2 text-destructive">
          （连接失败：{{ store.error }}）
        </span>
      </span>
      <span v-if="store.lastUpdatedAt" class="ml-auto hidden sm:inline">
        上次更新 {{ new Date(store.lastUpdatedAt).toLocaleTimeString() }}
      </span>
    </div>

    <div v-if="!store.runs.length && !store.error" class="py-20 text-center text-sm text-muted-foreground">
      暂无回测记录，启动回测后此处自动出现。
    </div>

    <div class="flex flex-col gap-2.5">
      <RunCard v-for="r in store.runs" :key="r.name" :run="r" @source="store.sourceName = $event" />
    </div>
  </main>
</template>