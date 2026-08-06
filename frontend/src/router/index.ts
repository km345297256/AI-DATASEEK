import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const MainLayout = () => import('@/pages/MainLayout.vue')
const HomePage = () => import('@/pages/HomePage.vue')
const ChatPage = () => import('@/pages/ChatPage.vue')
const ShareLayout = () => import('@/pages/ShareLayout.vue')
const SharePage = () => import('@/pages/SharePage.vue')
const PluginsPage = () => import('@/pages/PluginsPage.vue')
const AdminPage = () => import('@/pages/AdminPage.vue')
const DatasetSeekPage = () => import('@/pages/DatasetSeekPage.vue')

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
        component: PluginsPage,
      },
      {
        path: 'admin',
        component: AdminPage,
      },
      {
        path: 'datasets',
        redirect: '/chat',
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
    redirect: '/chat',
  },
  {
    path: '/dataset/setup',
    redirect: '/chat',
  },
  {
    path: '/dataset/seek',
    redirect: '/chat',
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

let rendererConfigsLoaded = false

router.afterEach(() => {
  if (rendererConfigsLoaded) return

  rendererConfigsLoaded = true
  void Promise.all([
    import('@/api/renderer'),
    import('@/renderers/registry'),
  ])
    .then(([{ listRendererConfigs }, { mergeRendererConfigs }]) => listRendererConfigs().then(mergeRendererConfigs))
    .catch((error) => {
      rendererConfigsLoaded = false
      console.warn('Failed to preload renderer configs:', error)
    })
})

export default router
