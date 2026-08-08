import type { ToolContent } from '@/types/message';

const LOCAL_PREVIEW_HOSTS = new Set([
  'localhost',
  '127.0.0.1',
  '0.0.0.0',
  '::1',
  '[::1]',
]);

const LOCAL_PREVIEW_BROWSER_FUNCTIONS = new Set([
  'browser_navigate',
  'browser_restart',
]);

const explicitPreviewUrl = (tool: ToolContent): string | undefined => {
  if (tool.name !== 'preview') return undefined;
  const value = tool.content?.url ?? tool.args?.url;
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
};

/**
 * Return a sandbox-local page URL that is safe to hand to the authenticated
 * preview proxy. Third-party browser pages deliberately remain Browser tools.
 */
export const localBrowserPreviewUrl = (tool: ToolContent): string | undefined => {
  if (
    tool.name !== 'browser'
    || tool.status !== 'called'
    || !LOCAL_PREVIEW_BROWSER_FUNCTIONS.has(tool.function)
  ) {
    return undefined;
  }

  const rawUrl = tool.args?.url;
  if (typeof rawUrl !== 'string' || !rawUrl.trim()) return undefined;

  try {
    const parsed = new URL(rawUrl.trim());
    if (!['http:', 'https:'].includes(parsed.protocol)) return undefined;
    if (parsed.username || parsed.password) return undefined;
    if (!LOCAL_PREVIEW_HOSTS.has(parsed.hostname.toLowerCase())) return undefined;
    return rawUrl.trim();
  } catch {
    return undefined;
  }
};

const derivedPreviewTool = (tool: ToolContent): ToolContent | undefined => {
  const url = localBrowserPreviewUrl(tool);
  if (!url) return undefined;
  return {
    ...tool,
    name: 'preview',
    function: 'preview_show',
    args: { url },
    content: {
      url,
      title: tool.args?.title || url,
    },
  };
};

/**
 * Pick the user-facing workspace for one user turn. An explicit preview wins
 * even when later verification emits file/browser tools. Legacy turns that
 * never called preview_show may fall back to a strictly local browser URL.
 */
export const selectPreferredWorkspaceTool = (
  turnTools: ToolContent[],
  fallback?: ToolContent,
  options: { allowLocalBrowserPreview?: boolean } = {},
): ToolContent | undefined => {
  for (let index = turnTools.length - 1; index >= 0; index -= 1) {
    if (explicitPreviewUrl(turnTools[index])) return turnTools[index];
  }

  if (options.allowLocalBrowserPreview !== false) {
    for (let index = turnTools.length - 1; index >= 0; index -= 1) {
      const preview = derivedPreviewTool(turnTools[index]);
      if (preview) return preview;
    }
  }

  return fallback;
};
