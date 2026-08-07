import { createRouter, createWebHistory } from 'vue-router'
import type { RouteLocationNormalized } from 'vue-router'
import {
  buildSub2ApiLoginUrl,
  captureExternalAuthHandoff,
  completeExternalAuthHandoff,
  getStoredToken,
  hydrateStoredExternalAuthToken,
  isExternalAuthHydrationSuppressed,
} from '../api/auth'
import { getCachedClientConfig } from '../api/config'

const EXTERNAL_AUTH_QUERY_KEYS = new Set([
  'manus_access_token',
  'access_token',
  'auth_token',
  'token',
  'refresh_token',
  'token_type',
  'expires_in',
  'manus_model_id',
  'manus_api_key',
  'manus_api_base',
  'manus_model',
  'manus_model_provider',
])

function cleanExternalAuthLocation(to: RouteLocationNormalized) {
  let changed = false
  const query = { ...to.query }
  const rawHashParams = to.hash
    ? new URLSearchParams(to.hash.startsWith('#') ? to.hash.slice(1) : to.hash)
    : new URLSearchParams()
  const hasHandoffFields = Array.from(EXTERNAL_AUTH_QUERY_KEYS).some(
    (key) => key in query || rawHashParams.has(key),
  )

  for (const key of EXTERNAL_AUTH_QUERY_KEYS) {
    if (key in query) {
      delete query[key]
      changed = true
    }
  }
  if (hasHandoffFields && 'state' in query) {
    delete query.state
    changed = true
  }

  let hash = to.hash
  if (hash) {
    const hashParams = rawHashParams
    const originalHash = hashParams.toString()
    for (const key of EXTERNAL_AUTH_QUERY_KEYS) hashParams.delete(key)
    if (hasHandoffFields) hashParams.delete('state')
    const cleanHash = hashParams.toString()
    if (cleanHash !== originalHash) {
      hash = cleanHash ? `#${cleanHash}` : ''
      changed = true
    }
  }

  if (!changed) return null
  return { path: to.path, query, hash, replace: true }
}

function currentManusRedirectUrl(to: RouteLocationNormalized): string {
  const url = new URL(to.fullPath || '/', window.location.origin)
  const hasHandoffFields = Array.from(EXTERNAL_AUTH_QUERY_KEYS).some(
    (key) => url.searchParams.has(key),
  )
  for (const key of EXTERNAL_AUTH_QUERY_KEYS) url.searchParams.delete(key)
  if (hasHandoffFields) url.searchParams.delete('state')

  if (url.hash) {
    const hashParams = new URLSearchParams(url.hash.startsWith('#') ? url.hash.slice(1) : url.hash)
    const hasHashHandoffFields = Array.from(EXTERNAL_AUTH_QUERY_KEYS).some((key) => hashParams.has(key))
    for (const key of EXTERNAL_AUTH_QUERY_KEYS) hashParams.delete(key)
    if (hasHashHandoffFields) hashParams.delete('state')
    const cleanHash = hashParams.toString()
    url.hash = cleanHash ? `#${cleanHash}` : ''
  }
  return url.toString()
}

function redirectToSub2ApiLogin(loginUrl: string, to: RouteLocationNormalized): void {
  const url = buildSub2ApiLoginUrl(loginUrl, currentManusRedirectUrl(to))
  window.location.replace(url)
}

// Pages are lazy-loaded so heavy dependencies (Monaco, NoVNC, Claw assets)
// stay out of the initial bundle.
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/library',
      component: () => import('../pages/MainLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          component: () => import('../pages/LibraryPage.vue'),
          meta: { requiresAuth: true }
        }
      ]
    },
    {
      path: '/project/:projectId',
      component: () => import('../pages/MainLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          component: () => import('../pages/ProjectPage.vue'),
          meta: { requiresAuth: true }
        }
      ]
    },
    {
      path: '/chat',
      component: () => import('../pages/MainLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          component: () => import('../pages/HomePage.vue'),
          alias: ['/', '/home'],
          meta: { requiresAuth: true }
        },
        {
          path: 'claw',
          component: () => import('../pages/ClawPage.vue'),
          meta: { requiresAuth: true }
        },
        {
          path: ':sessionId',
          component: () => import('../pages/ChatPage.vue'),
          meta: { requiresAuth: true }
        }
      ]
    },
    {
      path: '/share',
      component: () => import('../pages/ShareLayout.vue'),
      children: [
        {
          path: ':sessionId',
          component: () => import('../pages/SharePage.vue'),
        }
      ]
    },
    {
      path: '/login',
      component: () => import('../pages/LoginPage.vue')
    }
  ]
})

// Global route guard
router.beforeEach(async (to, _, next) => {
  const requiresAuth = to.matched.some((record) => record.meta?.requiresAuth)
  // Capture and scrub fragment credentials before the first await. This does
  // not mutate auth/model state; query credentials are discarded, and a later
  // /auth/me check gates the atomic commit.
  const pendingExternalHandoff = captureExternalAuthHandoff()
  const clientConfig = await getCachedClientConfig()
  const authProvider = clientConfig?.auth_provider ?? null
  let importedExternalToken = false
  if (authProvider === 'sub2api') {
    importedExternalToken = pendingExternalHandoff
      ? await completeExternalAuthHandoff(pendingExternalHandoff)
      : await hydrateStoredExternalAuthToken()
  }
  const hasToken = importedExternalToken || !!getStoredToken()
  const sub2apiLoginUrl = clientConfig?.sub2api_login_url ?? null
  const externalAuthSuppressed = isExternalAuthHydrationSuppressed()

  // Always remove accepted/rejected fragment handoff values, including when
  // loading runtime config failed and the token could not be imported.
  const cleanLocation = cleanExternalAuthLocation(to)
  if (cleanLocation) {
    next(cleanLocation)
    return
  }

  if (requiresAuth) {
    if (authProvider === 'none' || authProvider === null) {
      next()
      return
    }

    if (!hasToken) {
      if (authProvider === 'sub2api' && sub2apiLoginUrl && !externalAuthSuppressed) {
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
    if (hasToken) {
      next('/')
      return
    }
    if (authProvider === 'sub2api' && sub2apiLoginUrl && !externalAuthSuppressed) {
      redirectToSub2ApiLogin(sub2apiLoginUrl, to)
      return
    }
  }

  next()
})
