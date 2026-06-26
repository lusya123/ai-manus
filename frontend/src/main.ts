import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import './assets/global.css'
import './assets/theme.css'
import './utils/toast'
import i18n from './composables/useI18n'
import { getStoredToken, hydrateExternalAuthToken } from './api/auth'
import { getCachedClientConfig } from './api/config'
import { hydrateAgentConfigFromUrl } from './api/agentConfig'
import { configure } from "vue-gtag"

// Lazy-load route views so production users do not download the whole app upfront.
const HomePage = () => import('./pages/HomePage.vue')
const ChatPage = () => import('./pages/ChatPage.vue')
const LoginPage = () => import('./pages/LoginPage.vue')
const MainLayout = () => import('./pages/MainLayout.vue')
const ClawPage = () => import('./pages/ClawPage.vue')
const SharePage = () => import('./pages/SharePage.vue')
const ShareLayout = () => import('./pages/ShareLayout.vue')

const EXTERNAL_AUTH_QUERY_KEYS = new Set([
  'manus_access_token',
  'access_token',
  'auth_token',
  'token',
  'refresh_token',
  'token_type',
  'expires_in',
  'manus_api_key',
  'manus_api_base',
  'manus_model',
  'manus_model_provider',
])

function cleanExternalAuthLocation(to: any) {
  let changed = false
  const query = { ...to.query }

  for (const key of EXTERNAL_AUTH_QUERY_KEYS) {
    if (key in query) {
      delete query[key]
      changed = true
    }
  }

  let hash = to.hash
  if (hash) {
    const hashValue = hash.startsWith('#') ? hash.slice(1) : hash
    const hashParams = new URLSearchParams(hashValue)
    const originalHash = hashParams.toString()

    for (const key of EXTERNAL_AUTH_QUERY_KEYS) {
      hashParams.delete(key)
    }

    const cleanHash = hashParams.toString()
    if (cleanHash !== originalHash) {
      hash = cleanHash ? `#${cleanHash}` : ''
      changed = true
    }
  }

  if (!changed) {
    return null
  }

  return {
    path: to.path,
    query,
    hash,
    replace: true,
  }
}

function currentManusRedirectUrl(to: any): string {
  const url = new URL(to.fullPath || '/', window.location.origin)
  for (const key of EXTERNAL_AUTH_QUERY_KEYS) {
    url.searchParams.delete(key)
  }

  if (url.hash) {
    const hashValue = url.hash.startsWith('#') ? url.hash.slice(1) : url.hash
    const hashParams = new URLSearchParams(hashValue)
    let hashChanged = false
    for (const key of EXTERNAL_AUTH_QUERY_KEYS) {
      if (hashParams.has(key)) {
        hashParams.delete(key)
        hashChanged = true
      }
    }
    if (hashChanged) {
      const cleanHash = hashParams.toString()
      url.hash = cleanHash ? `#${cleanHash}` : ''
    }
  }

  return url.toString()
}

function redirectToSub2ApiLogin(loginUrl: string, to: any): void {
  const url = new URL(loginUrl, window.location.origin)
  url.searchParams.set('redirect_uri', currentManusRedirectUrl(to))
  window.location.replace(url.toString())
}

// Create router
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { 
      path: '/chat', 
      component: MainLayout,
      meta: { requiresAuth: true },
      children: [
        { 
          path: '', 
          component: HomePage, 
          alias: ['/', '/home'],
          meta: { requiresAuth: true }
        },
        {
          path: 'claw',
          component: ClawPage,
          meta: { requiresAuth: true }
        },
        { 
          path: ':sessionId', 
          component: ChatPage,
          meta: { requiresAuth: true }
        }
      ]
    },
    {
      path: '/share',
      component: ShareLayout,
      children: [
        {
          path: ':sessionId',
          component: SharePage,
        }
      ]
    },
    { 
      path: '/login', 
      component: LoginPage
    }
  ]
})

// Global route guard
router.beforeEach(async (to, _, next) => {
  const requiresAuth = to.matched.some((record: any) => record.meta?.requiresAuth)
  const hasToken = !!getStoredToken()
  const clientConfig = await getCachedClientConfig()
  const authProvider = clientConfig?.auth_provider ?? null
  const hasAgentConfig = hydrateAgentConfigFromUrl()
  const hasExternalToken = authProvider === 'sub2api' ? hydrateExternalAuthToken() : false
  const hasValidToken = hasExternalToken || hasToken
  const sub2apiLoginUrl = clientConfig?.sub2api_login_url ?? null

  if (hasExternalToken || hasAgentConfig) {
    const cleanLocation = cleanExternalAuthLocation(to)
    if (cleanLocation) {
      next(cleanLocation)
      return
    }
  }

  if (requiresAuth) {
    if (authProvider === 'none' || authProvider === null) {
      next()
      return
    }
    
    if (!hasValidToken) {
      if (authProvider === 'sub2api' && sub2apiLoginUrl) {
        redirectToSub2ApiLogin(sub2apiLoginUrl, to)
        return
      }
      next({
        path: '/login',
        query: { redirect: to.fullPath }
      })
      return
    }
  }
  
  if (to.path === '/login') {
    if (authProvider === 'none') {
      next('/')
      return
    }
    if (hasValidToken) {
      next('/')
      return
    }
    if (authProvider === 'sub2api' && sub2apiLoginUrl) {
      redirectToSub2ApiLogin(sub2apiLoginUrl, to)
      return
    }
  }

  next()
})

// Preload client runtime config and initialize Google Analytics.
void getCachedClientConfig().then((config) => {
  if (config?.google_analytics_id) {
    configure({ tagId: config.google_analytics_id })
  }
})

const app = createApp(App)

app.use(router)
app.use(i18n)
app.mount('#app') 
