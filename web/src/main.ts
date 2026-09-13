import { createPinia } from 'pinia'
import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import { router } from './router'
import { initTheme } from '@/lib/theme'

// 应用主题：默认深色，读取 localStorage 偏好（见 lib/theme.ts）
initTheme()

createApp(App).use(createPinia()).use(router).mount('#app')