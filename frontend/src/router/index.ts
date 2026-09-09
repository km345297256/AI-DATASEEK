import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { installFilePanelRouteLifecycle } from '@/composables/useFilePanel'

const MainLayout = () => import('@/pages/MainLayout.vue')
const HomePage = () => import('@/pages/HomePage.vue')
const ChatPage = () => import('@/pages/ChatPage.vue')
const ShareLayout = () => import('@/pages/ShareLayout.vue')
const SharePage = () => import('@/pages/SharePage.vue')
const PluginsPage = () => import('@/pages/PluginsPage.vue')
const AdminPage = () => import('@/pages/AdminPage.vue')
const DatasetSeekPage = () => import('@/pages/DatasetSeekPage.vue')
const DatasetSetupPage = () => import('@/pages/DatasetSetupPage.vue')
const DatasetManagementPage = () => import('@/pages/DatasetManagementPage.vue')

const routes: RouteRecordRaw[] = [
  {
    path: '/chat',
    component: MainLayout,
    children: [
      {
        path: '',
        component: HomePage,
        alias: ['/', '/home'],
      },
      {
        path: 'plugins',
        alias: '/plugins',
        component: PluginsPage,
      },
      {
        path: 'admin',
        component: AdminPage,
      },
      {
        path: 'datasets',
        alias: '/datasets',
        component: DatasetManagementPage,
      },
      {
        path: 'admin/tasks/:sessionId/replay',
        component: SharePage,
        meta: { adminReplay: true },
      },
      {
        path: ':sessionId',
        component: ChatPage,
      },
    ],
  },
  {
    path: '/dataset',
    redirect: '/datasets',
  },
  {
    path: '/dataset/setup',
    component: DatasetSetupPage,
  },
  {
    path: '/dataset/seek',
    redirect: '/dataset/setup',
  },
  {
    path: '/dataset/seek/:datasetId',
    component: DatasetSeekPage,
  },
  {
    path: '/share',
    component: ShareLayout,
    children: [
      {
        path: ':sessionId',
        component: SharePage,
      },
    ],
  },
  {
    path: '/login',
    redirect: '/',
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

installFilePanelRouteLifecycle(router)

export default router
