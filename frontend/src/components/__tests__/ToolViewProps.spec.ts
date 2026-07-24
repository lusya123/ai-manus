import { describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import SearchToolView from '../toolViews/SearchToolView.vue';
import FileToolView from '../toolViews/FileToolView.vue';
import ShellToolView from '../toolViews/ShellToolView.vue';
import McpToolView from '../toolViews/McpToolView.vue';
import type { ToolContent } from '../../types/message';

vi.mock('@/components/ui/MonacoEditor.vue', () => ({
  default: { template: '<div />' },
}));

const fragmentToolViews = [
  ['search', SearchToolView],
  ['file', FileToolView],
  ['shell', ShellToolView],
  ['mcp', McpToolView],
] as const;

const toolContent: ToolContent = {
  tool_call_id: 'search-1',
  name: 'search',
  function: 'info_search_web',
  args: { query: 'test' },
  content: { results: [] },
  status: 'called',
  timestamp: 1,
};

describe('fragment tool view props', () => {
  it.each(fragmentToolViews)('%s explicitly accepts the shared-page context', (_name, component) => {
    const props = (component as unknown as { props?: Record<string, unknown> }).props;

    expect(props).toHaveProperty('isShare');
  });

  it('does not report isShare as an extraneous attribute', () => {
    const warnings: string[] = [];

    mount(SearchToolView, {
      props: {
        sessionId: 'session-1',
        toolContent,
        live: false,
        isShare: true,
      },
      global: {
        config: {
          warnHandler: (message) => warnings.push(message),
        },
      },
    });

    expect(warnings.some((message) => message.includes('Extraneous non-props attributes'))).toBe(false);
  });
});
