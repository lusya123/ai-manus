import { describe, expect, it } from 'vitest';
import type { ToolContent } from '@/types/message';
import {
  localBrowserPreviewUrl,
  selectPreferredWorkspaceTool,
} from '@/utils/workspaceTool';

const tool = (overrides: Partial<ToolContent>): ToolContent => ({
  timestamp: 1,
  tool_call_id: 'tool-1',
  name: 'file',
  function: 'file_read',
  args: {},
  status: 'called',
  ...overrides,
});

describe('workspace tool selection', () => {
  it('keeps the current-turn explicit preview ahead of later verification tools', () => {
    const preview = tool({
      tool_call_id: 'preview-1',
      name: 'preview',
      function: 'preview_show',
      args: { url: 'http://localhost:4173/' },
      content: { url: 'http://localhost:4173/' },
    });
    const laterFile = tool({
      timestamp: 2,
      tool_call_id: 'file-2',
    });

    expect(selectPreferredWorkspaceTool([preview, laterFile], laterFile)).toBe(preview);
  });

  it('derives an interactive preview from a completed local browser navigation', () => {
    const browser = tool({
      tool_call_id: 'browser-1',
      name: 'browser',
      function: 'browser_navigate',
      args: { url: 'http://127.0.0.1:8099/index.html' },
    });

    const selected = selectPreferredWorkspaceTool([browser], browser);

    expect(selected).toMatchObject({
      tool_call_id: 'browser-1',
      name: 'preview',
      function: 'preview_show',
      args: { url: 'http://127.0.0.1:8099/index.html' },
      content: { url: 'http://127.0.0.1:8099/index.html' },
    });
  });

  it.each([
    'https://example.com/',
    'https://localhost.evil.example/',
    'http://user:secret@localhost:4173/',
    'file:///home/ubuntu/index.html',
  ])('does not turn an untrusted browser URL into a preview: %s', (url) => {
    const browser = tool({
      name: 'browser',
      function: 'browser_navigate',
      args: { url },
    });

    expect(localBrowserPreviewUrl(browser)).toBeUndefined();
    expect(selectPreferredWorkspaceTool([browser], browser)).toBe(browser);
  });

  it('requires the local browser navigation to have completed', () => {
    const browser = tool({
      name: 'browser',
      function: 'browser_navigate',
      args: { url: 'http://[::1]:4173/' },
      status: 'calling',
    });

    expect(localBrowserPreviewUrl(browser)).toBeUndefined();
  });

  it('accepts a completed IPv6 loopback navigation', () => {
    const browser = tool({
      name: 'browser',
      function: 'browser_navigate',
      args: { url: 'http://[::1]:4173/' },
    });

    expect(localBrowserPreviewUrl(browser)).toBe('http://[::1]:4173/');
  });

  it('can disable the legacy localhost fallback for live tool activity', () => {
    const browser = tool({
      name: 'browser',
      function: 'browser_navigate',
      args: { url: 'http://localhost:4173/' },
    });
    const currentTool = tool({
      tool_call_id: 'browser-click',
      name: 'browser',
      function: 'browser_click',
    });

    expect(selectPreferredWorkspaceTool(
      [browser, currentTool],
      currentTool,
      { allowLocalBrowserPreview: false },
    )).toBe(currentTool);
  });
});
