<script setup lang="ts">
// 策略源码查看对话框（shadcn Dialog + ScrollArea）
import { watch, ref } from 'vue'
import { api } from '@/api/client'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'

const props = defineProps<{ name: string | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const open = ref(false)
const source = ref('')
const filePath = ref('')
const error = ref('')
const loading = ref(false)

watch(
  () => props.name,
  async (name) => {
    if (!name) {
      open.value = false
      return
    }
    open.value = true
    loading.value = true
    error.value = ''
    source.value = ''
    try {
      const d = await api.source(name)
      if (d.exists && d.source != null) {
        source.value = d.source
        filePath.value = d.path ?? ''
      } else {
        error.value = `源码文件不存在：${d.path ?? '（未记录策略路径）'}`
      }
    } catch (e) {
      error.value = `源码加载失败：${(e as Error).message}`
    } finally {
      loading.value = false
    }
  },
)
</script>

<template>
  <Dialog :open="open" @update:open="(v: boolean) => { if (!v) emit('close') }">
    <DialogContent class="max-w-4xl p-4 sm:p-6">
      <DialogHeader class="min-w-0">
        <DialogTitle class="truncate font-mono text-xs sm:text-sm" :title="filePath">
          {{ filePath || props.name }}
        </DialogTitle>
      </DialogHeader>

      <div v-if="loading" class="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
        <span class="size-3.5 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-muted-foreground" />
        加载中…
      </div>
      <div v-else-if="error" class="py-10 text-center text-sm text-destructive">{{ error }}</div>
      <ScrollArea v-else class="max-h-[70vh] overflow-auto rounded-md border bg-slate-950 p-2 sm:p-3">
        <pre class="whitespace-pre-wrap break-all font-mono text-xs leading-relaxed text-slate-100">{{ source || '（空文件）' }}</pre>
      </ScrollArea>
    </DialogContent>
  </Dialog>
</template>
