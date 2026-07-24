import { beforeEach, describe, expect, it, vi } from 'vitest';
import { defineComponent, nextTick } from 'vue';
import { flushPromises, mount } from '@vue/test-utils';
import HomePage from '../HomePage.vue';
import { i18n } from '../../composables/useI18n';
import {
  CUSTOM_STORED_MODEL_ID,
  saveSelectedModelId,
  saveStoredAgentConfig,
} from '../../api/agentConfig';
import type { ClientConfigResponse } from '../../api/config';

const { routerPush } = vi.hoisted(() => ({ routerPush: vi.fn() }));

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
}));

vi.mock('../../api/agent', () => ({
  createSession: vi.fn(),
}));

vi.mock('../../api/config', () => ({
  getCachedClientConfig: vi.fn(),
  getCachedAuthProvider: vi.fn().mockResolvedValue('none'),
}));

import { createSession } from '../../api/agent';
import { getCachedClientConfig } from '../../api/config';

const clientConfig: ClientConfigResponse = {
  auth_provider: 'none',
  registration_enabled: false,
  show_github_button: false,
  github_repository_url: '',
  google_analytics_id: null,
  claw_enabled: false,
  supported_byok_providers: ['openai', 'anthropic', 'deepseek', 'ollama'],
};

const ChatBoxStub = defineComponent({
  name: 'ChatBox',
  props: {
    modelValue: { type: String, default: '' },
    attachments: { type: Array, default: () => [] },
    selectedModelId: { type: String, default: '' },
    modelOptions: { type: Array, default: () => [] },
  },
  emits: ['update:modelValue', 'update:attachments', 'update:selectedModelId', 'submit'],
  template: '<button data-testid="submit" @click="$emit(\'submit\')">submit</button>',
});

const SimpleBarStub = defineComponent({
  name: 'SimpleBar',
  template: '<div><slot /></div>',
});

describe('HomePage model settings synchronization', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    routerPush.mockReset();
    vi.mocked(createSession).mockReset();
    vi.mocked(getCachedClientConfig).mockResolvedValue(clientConfig);
  });

  it('uses model settings saved while the page remains mounted', async () => {
    vi.mocked(createSession).mockResolvedValue({ session_id: 'new-session' });
    const wrapper = mount(HomePage, {
      global: {
        plugins: [i18n],
        stubs: { ChatBox: ChatBoxStub, SimpleBar: SimpleBarStub },
      },
    });
    await flushPromises();

    const customConfig = {
      model_name: 'fresh-model',
      model_provider: 'openai',
      api_base: 'https://fresh.test/v1',
      api_key: 'fresh-secret',
    };
    saveStoredAgentConfig(customConfig);
    saveSelectedModelId(CUSTOM_STORED_MODEL_ID);
    await nextTick();

    const chatBox = wrapper.getComponent(ChatBoxStub);
    expect(chatBox.props('selectedModelId')).toBe(CUSTOM_STORED_MODEL_ID);
    chatBox.vm.$emit('update:modelValue', 'new task');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    expect(createSession).toHaveBeenCalledWith(customConfig);
    expect(routerPush).toHaveBeenCalledWith(expect.objectContaining({ path: '/chat/new-session' }));
    wrapper.unmount();
  });
});
