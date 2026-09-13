// 前端路由：列表首页 + run 详情页（history 模式，SPA 无整页刷新）
import { createRouter, createWebHistory } from 'vue-router'
import RunListView from '@/views/RunListView.vue'
import RunDetailView from '@/views/RunDetailView.vue'

export const router = createRouter({
  // 本地看板由 server.py 静态托管，非 API 路径已回退 index.html（见 server.py SPA 路由）
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'runs', component: RunListView },
    {
      path: '/run/:name',
      name: 'run-detail',
      component: RunDetailView,
      props: true,
    },
  ],
})

export default router