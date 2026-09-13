<script setup lang="ts">
// 主视图外壳：header + 全局源码 Dialog；路由出口渲染首页/详情页
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRouter } from 'vue-router'
import { Moon, RefreshCw, Sun } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useRunsStore } from '@/stores/runs'
import { applyTheme, getTheme, type Theme } from '@/lib/theme'
import SourceDialog from '@/components/SourceDialog.vue'

const store = useRunsStore()
const router = useRouter()

const theme = ref<Theme>(getTheme())
const isDark = computed(() => theme.value === 'dark')
function toggleTheme() {
  theme.value = isDark.value ? 'light' : 'dark'
  applyTheme(theme.value)
}

onMounted(() => store.start())
onBeforeUnmount(() => store.stop())

// 路由守卫：仅 2 条顶层路由，导航后回到列表顶部（浏览器在内存中记忆滚动位置，这里在顶层统一处理）
router.afterEach(() => {
  window.scrollTo({ top: 0, behavior: 'auto' })
})
</script>

<template>
  <div class="min-h-screen bg-muted/40">
    <header class="sticky top-0 z-30 flex flex-wrap items-center gap-x-2 gap-y-1.5 border-b bg-background/90 px-3 py-2.5 backdrop-blur sm:gap-3 sm:px-5 sm:py-3">
      <h1 class="text-base font-semibold tracking-tight sm:text-lg">
        <RouterLink
          to="/"
          class="rounded-sm transition-colors hover:text-foreground/80 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          PTrade 回测看板
        </RouterLink>
      </h1>
      <Badge v-if="store.runningCount" variant="destructive" class="gap-1 px-1.5 py-0 text-[10px] sm:px-2.5 sm:text-xs">
        <span class="size-1.5 animate-pulse rounded-full bg-current" />
        <span class="sm:hidden">{{ store.runningCount }} 进行中</span>
        <span class="hidden sm:inline">{{ store.runningCount }} 个回测进行中</span>
      </Badge>
      <div class="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground sm:gap-3">
        <span v-if="store.lastUpdatedAt" class="hidden sm:inline">
          每 {{ store.intervalMs / 1000 }}s 自动更新 · 上次
          {{ new Date(store.lastUpdatedAt).toLocaleTimeString() }}
        </span>
        <Button
          variant="ghost"
          size="icon-sm"
          :title="isDark ? '切换到浅色' : '切换到深色'"
          @click="toggleTheme"
        >
          <Sun v-if="isDark" class="size-4" />
          <Moon v-else class="size-4" />
        </Button>
        <Button variant="outline" size="sm" class="gap-1.5 px-2 sm:px-3" @click="store.refresh()">
          <RefreshCw class="size-3.5" />
          刷新
        </Button>
      </div>
    </header>

    <RouterView v-slot="{ Component }">
      <component :is="Component" />
    </RouterView>

    <SourceDialog :name="store.sourceName" @close="store.sourceName = null" />
  </div>
</template>