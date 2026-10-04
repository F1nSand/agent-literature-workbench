import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import LibraryView from '../views/LibraryView.vue'
import StageView from '../views/StageView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: HomeView, meta: { title: '概览' } },
    { path: '/documents', component: LibraryView, meta: { title: '文献库' } },
    { path: '/documents/:id', component: () => import('../views/ReaderView.vue'), meta: { title: '论文阅读' } },
    { path: '/notes', redirect: '/' },
    { path: '/study/:id', redirect: (to) => ({ path: `/documents/${encodeURIComponent(String(to.params.id))}`, query: to.query }) },
    { path: '/topics/:pathMatch(.*)*', redirect: '/documents' },
    { path: '/artifacts/:pathMatch(.*)*', redirect: '/documents' },
    { path: '/settings', component: StageView, props: { title: '设置', eyebrow: 'PREFERENCES', description: '了解工作台的数据存储与模型使用方式。', emptyTitle: '数据与隐私', emptyDescription: '文献原件和研读记录保存在本机。使用云端模型时，相关内容会发送给所选服务商；访问凭据由本机服务读取，不会显示在页面中。', icon: 'settings', isSettings: true }, meta: { title: '设置' } },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

export default router
