<template>
  <Dialog v-model:open="open">
    <DialogContent class="w-[420px] max-w-[calc(100vw-32px)] rounded-[16px] border-0 p-0 shadow-menu">
      <div class="p-5 sm:p-6">
        <DialogHeader>
          <DialogTitle class="text-[18px] font-semibold text-[var(--text-primary)]">
            {{ t('Update Password') }}
          </DialogTitle>
          <DialogDescription class="text-[13px] text-[var(--text-tertiary)]">
            {{ t('Use at least 6 characters for your new password.') }}
          </DialogDescription>
        </DialogHeader>

        <form class="mt-5 space-y-4" @submit.prevent="handleSubmit">
          <label
            v-for="field in fields"
            :key="field.id"
            class="block space-y-2"
          >
            <span class="text-[13px] font-medium text-[var(--text-secondary)]">
              {{ t(field.label) }}
            </span>
            <input
              :id="field.id"
              v-model="field.model.value"
              :data-testid="field.id"
              type="password"
              autocomplete="off"
              class="h-10 w-full rounded-[10px] bg-[var(--fill-tsp-white-main)] px-3 text-sm text-[var(--text-primary)] outline-none placeholder:text-[var(--text-disable)] focus:ring-[1.5px] focus:ring-[var(--border-input-active)]"
              :placeholder="t(field.placeholder)"
              :disabled="isLoading"
            >
          </label>

          <p
            v-if="confirmPassword && newPassword !== confirmPassword"
            class="text-[13px] text-[var(--function-error)]"
          >
            {{ t('Passwords do not match') }}
          </p>

          <DialogFooter class="mt-6 gap-2 sm:gap-2">
            <button
              type="button"
              class="h-9 min-w-[88px] rounded-[10px] border border-[var(--border-btn-main)] px-3 text-sm font-medium text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]"
              :disabled="isLoading"
              @click="open = false"
            >
              {{ t('Cancel') }}
            </button>
            <button
              type="submit"
              data-testid="change-password-submit"
              class="h-9 min-w-[88px] rounded-[10px] bg-[var(--Button-primary-black)] px-3 text-sm font-medium text-[var(--text-onblack)] disabled:cursor-not-allowed disabled:opacity-50"
              :disabled="!isFormValid"
            >
              {{ isLoading ? t('Processing...') : t('Confirm') }}
            </button>
          </DialogFooter>
        </form>
      </div>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { changePassword } from '@/api/auth'
import { showErrorToast, showSuccessToast } from '@/utils/toast'

const emit = defineEmits<{ changed: [] }>()
const { t } = useI18n()
const open = ref(false)
const isLoading = ref(false)
const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')

const fields = [
  {
    id: 'current-password',
    label: 'Current Password',
    placeholder: 'Enter current password',
    model: currentPassword,
  },
  {
    id: 'new-password',
    label: 'New Password',
    placeholder: 'Enter new password',
    model: newPassword,
  },
  {
    id: 'confirm-new-password',
    label: 'Confirm New Password',
    placeholder: 'Enter new password again',
    model: confirmPassword,
  },
] as const

const isFormValid = computed(() => (
  !isLoading.value
  && currentPassword.value.length > 0
  && newPassword.value.length >= 6
  && newPassword.value === confirmPassword.value
))

const reset = () => {
  currentPassword.value = ''
  newPassword.value = ''
  confirmPassword.value = ''
  isLoading.value = false
}

const handleSubmit = async () => {
  if (!isFormValid.value) return
  isLoading.value = true
  try {
    await changePassword({
      old_password: currentPassword.value,
      new_password: newPassword.value,
    })
    showSuccessToast(t('Password change successful'))
    open.value = false
    reset()
    emit('changed')
  } catch (error: unknown) {
    const err = error as { response?: { data?: { message?: string } }; message?: string }
    showErrorToast(
      err.response?.data?.message || err.message || t('Password change failed'),
    )
  } finally {
    isLoading.value = false
  }
}

defineExpose({
  open: () => {
    reset()
    open.value = true
  },
})
</script>
