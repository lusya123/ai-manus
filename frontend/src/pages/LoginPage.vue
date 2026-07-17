<template>
  <div class="w-full min-h-[100vh] relative bg-[var(--background-gray-main)] dark:bg-[#050505]">
    <div class="sticky top-0 left-0 w-full z-[10] px-[48px] max-sm:px-[12px] max-sm:bg-[var(--background-gray-login)]">
      <div class="w-full h-[60px] mx-auto flex items-center justify-between text-[var(--text-primary)]">
        <a href="/">
          <div class="flex gap-0.5 w-fit items-center">
            <Bot :size="30" class="text-[var(--icon-primary)]" />
            <ManusLogoTextIcon :width="74.1" :height="32" />
          </div>
        </a>
      </div>
    </div>
    <div
      class="relative z-[1] flex flex-col justify-center items-center min-h-[100vh] pt-[20px] pb-[60px] -mt-[60px] max-sm:pt-[80px] max-sm:pb-[80px] max-sm:mt-0 max-sm:min-h-[calc(100vh-60px)] max-sm:justify-start">
      <div class="w-full max-w-[720px] pt-[24px] mb-[40px] max-sm:pt-[0px]">
        <div class="flex flex-col items-center gap-[20px] relative" style="z-index:1">
          <div class="w-[80px] h-[80px] text-[var(--icon-primary)] max-sm:w-[64px] max-sm:h-[64px]">
            <Bot :size="80" />
          </div>
          <h1 class="text-[20px] font-bold text-center text-[var(--text-primary)] max-sm:text-[18px]">
            {{ 
              isResettingPassword ? t('Reset Password') 
              : isRegistering ? t('Register to Manus') 
              : t('Login to Manus') 
            }}
          </h1>
        </div>
      </div>
      <div v-if="sub2apiLoginUrl" class="w-full max-w-[360px] flex flex-col items-center gap-4 px-5">
        <p class="text-sm text-center text-[var(--text-secondary)]">
          {{ t('You are signed out of Manus') }}
        </p>
        <button
          type="button"
          class="w-full h-10 rounded-[10px] bg-[var(--Button-primary-black)] text-[var(--text-onblack)] font-medium hover:opacity-90"
          @click="continueWithSub2Api"
        >
          {{ t('Continue with Sub2API') }}
        </button>
      </div>
      <LoginForm v-else-if="!isRegistering && !isResettingPassword"
        @success="handleLoginSuccess" 
        @switch-to-register="switchToRegister" 
        @switch-to-reset="switchToReset" />
      <RegisterForm v-else-if="isRegistering && !isResettingPassword" 
        @success="handleLoginSuccess" 
        @switch-to-login="switchToLogin" />
      <ResetPasswordForm v-else-if="isResettingPassword" 
        @back-to-login="switchToLogin" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Bot } from 'lucide-vue-next'
import ManusLogoTextIcon from '@/components/icons/ManusLogoTextIcon.vue'
import LoginForm from '@/components/login/LoginForm.vue'
import RegisterForm from '@/components/login/RegisterForm.vue'
import ResetPasswordForm from '@/components/login/ResetPasswordForm.vue'
import { useAuth } from '@/api'
import { buildSub2ApiLoginUrl } from '@/api/auth'
import { getCachedClientConfig } from '@/api/config'

const { t } = useI18n()

const router = useRouter()
const { isAuthenticated } = useAuth()

// Form state for header display
const isRegistering = ref(false)
const isResettingPassword = ref(false)
const sub2apiLoginUrl = ref<string | null>(null)

const continueWithSub2Api = () => {
  if (!sub2apiLoginUrl.value) return
  const redirect = router.currentRoute.value.query.redirect
  const returnPath = typeof redirect === 'string' ? redirect : '/'
  const requestedReturnUrl = new URL(returnPath, window.location.origin)
  const safeReturnUrl = requestedReturnUrl.origin === window.location.origin
    ? requestedReturnUrl
    : new URL('/', window.location.origin)
  window.location.assign(buildSub2ApiLoginUrl(
    sub2apiLoginUrl.value,
    safeReturnUrl.toString(),
  ))
}

// Switch to register mode
const switchToRegister = () => {
  isRegistering.value = true
  isResettingPassword.value = false
}

// Switch to login mode
const switchToLogin = () => {
  isRegistering.value = false
  isResettingPassword.value = false
}

// Switch to reset password mode
const switchToReset = () => {
  isRegistering.value = false
  isResettingPassword.value = true
}

// Handle successful login/registration
const handleLoginSuccess = () => {
    const redirect = router.currentRoute.value.query.redirect as string
    router.push(redirect || '/')
}

// Listen for authentication state changes
watch(isAuthenticated, (authenticated) => {
  if (authenticated) {
    handleLoginSuccess()
  }
})

// Check if already logged in when page loads
onMounted(async () => {
  const clientConfig = await getCachedClientConfig()
  if (clientConfig?.auth_provider === 'sub2api') {
    sub2apiLoginUrl.value = clientConfig.sub2api_login_url || null
  }
  if (isAuthenticated.value) {
    router.push('/')
  }
})
</script>
