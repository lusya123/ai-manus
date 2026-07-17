import { describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import ShellToolView from '../toolViews/ShellToolView.vue';
import type { ToolContent } from '../../types/message';

vi.mock('@/api/agent', () => ({
  viewShellSession: vi.fn(),
}));

describe('ShellToolView output escaping', () => {
  it('renders shell fields as text without creating executable DOM', async () => {
    const imagePayload = '<img src=x onerror="window.__shellXss = true">';
    const scriptPayload = '<script>window.__shellXss = true</script>';
    const toolContent: ToolContent = {
      tool_call_id: 'shell-1',
      name: 'shell',
      function: 'shell_view',
      args: { id: 'terminal-1' },
      content: {
        console: [{
          ps1: imagePayload,
          command: scriptPayload,
          output: `${imagePayload}\n${scriptPayload}`,
        }],
      },
      status: 'called',
      timestamp: 1,
    };

    const wrapper = mount(ShellToolView, {
      props: {
        sessionId: 'session-1',
        toolContent,
        live: false,
      },
    });
    await flushPromises();

    const shellConsole = wrapper.get('[data-testid="shell-console"]');
    expect(shellConsole.text()).toContain(imagePayload);
    expect(shellConsole.text()).toContain(scriptPayload);
    expect(shellConsole.find('img').exists()).toBe(false);
    expect(shellConsole.find('script').exists()).toBe(false);
    expect(shellConsole.find('[onerror]').exists()).toBe(false);
    expect(shellConsole.html()).toContain('&lt;img');
    expect(shellConsole.html()).toContain('&lt;script&gt;');
  });
});
