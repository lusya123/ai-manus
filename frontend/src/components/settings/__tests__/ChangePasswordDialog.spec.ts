import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import ChangePasswordDialog from '../ChangePasswordDialog.vue'
import { i18n } from '../../../composables/useI18n'

const { authMocks, toastMocks } = vi.hoisted(() => ({
  authMocks: { changePassword: vi.fn() },
  toastMocks: { showSuccessToast: vi.fn(), showErrorToast: vi.fn() },
}))

vi.mock('../../../api/auth', () => authMocks)
vi.mock('../../../utils/toast', () => toastMocks)

const PassThrough = defineComponent({ template: '<div><slot /></div>' })

const mountDialog = () => mount(ChangePasswordDialog, {
  global: {
    plugins: [i18n],
    stubs: {
      Dialog: PassThrough,
      DialogContent: PassThrough,
      DialogDescription: PassThrough,
      DialogFooter: PassThrough,
      DialogHeader: PassThrough,
      DialogTitle: PassThrough,
    },
  },
})

describe('ChangePasswordDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    authMocks.changePassword.mockResolvedValue({})
  })

  it('submits the existing change-password API and emits changed', async () => {
    const wrapper = mountDialog()
    await wrapper.find('[data-testid="current-password"]').setValue('old-secret')
    await wrapper.find('[data-testid="new-password"]').setValue('new-secret')
    await wrapper.find('[data-testid="confirm-new-password"]').setValue('new-secret')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(authMocks.changePassword).toHaveBeenCalledWith({
      old_password: 'old-secret',
      new_password: 'new-secret',
    })
    expect(wrapper.emitted('changed')).toHaveLength(1)
    expect(toastMocks.showSuccessToast).toHaveBeenCalledOnce()
  })

  it('keeps mismatched passwords from being submitted', async () => {
    const wrapper = mountDialog()
    await wrapper.find('[data-testid="current-password"]').setValue('old-secret')
    await wrapper.find('[data-testid="new-password"]').setValue('new-secret')
    await wrapper.find('[data-testid="confirm-new-password"]').setValue('different')

    expect(wrapper.get('[data-testid="change-password-submit"]').attributes('disabled')).toBeDefined()
    await wrapper.find('form').trigger('submit')
    expect(authMocks.changePassword).not.toHaveBeenCalled()
  })
})
