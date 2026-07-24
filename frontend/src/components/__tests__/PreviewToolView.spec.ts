import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import PreviewToolView from '../toolViews/PreviewToolView.vue';
import { i18n } from '../../composables/useI18n';
import type { ToolContent } from '../../types/message';

vi.mock('@/api/agent', () => ({
  createPreviewUrl: vi.fn(),
}));

import { createPreviewUrl } from '@/api/agent';

const toolContent: ToolContent = {
  tool_call_id: 'preview-1',
  name: 'preview',
  function: 'preview_show',
  args: { url: 'http://localhost:3000/' },
  status: 'called',
  timestamp: 1,
};

const mountPreview = (
  isShare: boolean,
  content: ToolContent = toolContent,
) => mount(PreviewToolView, {
  props: {
    sessionId: 'session-1',
    toolContent: content,
    live: false,
    isShare,
  },
  global: { plugins: [i18n] },
});

describe('PreviewToolView isolation', () => {
  beforeEach(() => {
    vi.mocked(createPreviewUrl).mockReset();
  });

  it('keeps a same-origin preview sandboxed and has no top-level link', async () => {
    vi.mocked(createPreviewUrl).mockResolvedValue({
      signed_url: '/api/v1/sessions/session-1/preview/token/3000/',
      expires_in: 900,
    });

    const wrapper = mountPreview(false);
    await flushPromises();

    const sandbox = wrapper.get('iframe').attributes('sandbox');
    expect(sandbox).not.toContain('allow-same-origin');
    expect(sandbox).not.toContain('allow-popups');
    expect(wrapper.find('a[target="_blank"]').exists()).toBe(false);
  });

  it('requests public preview access without owner auth on a shared page', async () => {
    vi.mocked(createPreviewUrl).mockResolvedValue({
      signed_url: '/api/v1/sessions/session-1/preview/token/3000/',
      expires_in: 900,
    });

    mountPreview(true, {
      ...toolContent,
      // This is the actual public mapper shape: arguments are always redacted,
      // while an approved local preview survives in the content allowlist.
      args: {},
      content: {
        url: 'http://localhost:3000/',
      },
    });
    await flushPromises();

    expect(createPreviewUrl).toHaveBeenCalledWith(
      'session-1',
      'http://localhost:3000/',
      { publicAccess: true },
    );
  });

  it('only offers a top-level link for a genuinely cross-origin URL', async () => {
    vi.mocked(createPreviewUrl).mockResolvedValue({
      signed_url: 'https://preview.example.test/',
      expires_in: 900,
    });

    const wrapper = mountPreview(false);
    await flushPromises();

    expect(wrapper.get('a[target="_blank"]').attributes('href')).toBe('https://preview.example.test/');
  });

  it('never opens a proxied preview as an unsandboxed top-level page', async () => {
    vi.mocked(createPreviewUrl).mockResolvedValue({
      signed_url: 'https://api.example.test/api/v1/sessions/session-1/preview/token/3000/',
      expires_in: 900,
    });

    const wrapper = mountPreview(false);
    await flushPromises();

    expect(wrapper.find('a[target="_blank"]').exists()).toBe(false);
  });
});
