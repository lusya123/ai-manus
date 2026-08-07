import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, nextTick } from 'vue'
import { flushPromises, shallowMount } from '@vue/test-utils'
import AccountSettings from '../AccountSettings.vue'
import { i18n } from '../../../composables/useI18n'

const { authMocks, configMocks } = vi.hoisted(() => ({
  authMocks: {
    currentUser: {
      __v_isRef: true,
      value: {
        id: 'user-1',
        fullname: 'Password User',
        email: 'user@example.com',
        role: 'user' as const,
        is_active: true,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      },
    },
    logout: vi.fn(),
    loadCurrentUser: vi.fn(),
  },
  configMocks: {
    getCachedAuthProvider: vi.fn(),
  },
}))

vi.mock('../../../composables/useAuth', () => ({
  useAuth: () => authMocks,
}))

vi.mock('../../../api/config', () => configMocks)

vi.mock('../../../api/auth', () => ({
  changeFullname: vi.fn(),
}))

const ChangePasswordDialogStub = defineComponent({
  name: 'ChangePasswordDialog',
  emits: ['changed'],
  setup(_, { expose }) {
    expose({ open: vi.fn() })
    return () => null
  },
})

const mountAccount = async (provider: string) => {
  configMocks.getCachedAuthProvider.mockResolvedValue(provider)
  const wrapper = shallowMount(AccountSettings, {
    global: {
      plugins: [i18n],
      stubs: { ChangePasswordDialog: ChangePasswordDialogStub },
    },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('AccountSettings password visibility', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the password action for password-backed accounts', async () => {
    const wrapper = await mountAccount('password')
    expect(wrapper.find('[data-testid="open-change-password"]').exists()).toBe(true)
    expect(wrapper.findComponent(ChangePasswordDialogStub).exists()).toBe(true)
  })

  it.each(['sub2api', 'none', 'local'])(
    'does not show the password action for %s auth',
    async (provider) => {
      const wrapper = await mountAccount(provider)
      expect(wrapper.find('[data-testid="open-change-password"]').exists()).toBe(false)
      expect(wrapper.findComponent(ChangePasswordDialogStub).exists()).toBe(false)
    },
  )
})
