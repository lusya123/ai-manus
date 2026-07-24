import type { ClientConfigResponse, ModelOptionResponse } from './config';

export interface StoredAgentConfig {
  model_id?: string;
  api_key?: string;
  api_base?: string;
  model_name?: string;
  model_provider?: string;
}

export interface CapturedAgentConfigHandoff {
  /** Whether the URL contained any model handoff fields, including rejected query fields. */
  hasConfigParams: boolean;
  /** A canonical fragment-only model selection, or null when the handoff was absent/invalid. */
  config: StoredAgentConfig | null;
}

export interface ChatModelOption extends ModelOptionResponse {
  is_system_default?: boolean;
  is_custom?: boolean;
}

const STORAGE_KEY = 'sub2api_agent_config';
const SELECTED_MODEL_STORAGE_KEY = 'manus_selected_model_id';
export const AGENT_CONFIG_CHANGED_EVENT = 'agent-config:changed';
export const SYSTEM_MODEL_ID = 'system-default';
export const CURRENT_SESSION_MODEL_ID = 'current-session-model';
export const CUSTOM_STORED_MODEL_ID = 'custom-stored-model';

const CONFIG_PARAM_MAP: Record<string, keyof StoredAgentConfig> = {
  manus_model_id: 'model_id',
  manus_api_key: 'api_key',
  manus_api_base: 'api_base',
  manus_model: 'model_name',
  manus_model_provider: 'model_provider',
};

function normalizeConfig(config: StoredAgentConfig): StoredAgentConfig {
  return Object.fromEntries(
    Object.entries(config)
      .map(([key, value]) => [key, typeof value === 'string' ? value.trim() : value])
      .filter(([, value]) => typeof value === 'string' && value.length > 0)
  ) as StoredAgentConfig;
}

function canonicalizeConfig(config: StoredAgentConfig): StoredAgentConfig {
  const normalized = normalizeConfig(config);
  const customFields = [
    normalized.api_key,
    normalized.api_base,
    normalized.model_name,
    normalized.model_provider,
  ];
  const hasAnyCustomField = customFields.some(Boolean);
  const hasCompleteCustomConfig = customFields.every(Boolean);

  if (normalized.model_id && !hasAnyCustomField) {
    return { model_id: normalized.model_id };
  }
  if (!normalized.model_id && hasCompleteCustomConfig) {
    return {
      api_key: normalized.api_key,
      api_base: normalized.api_base,
      model_name: normalized.model_name,
      model_provider: normalized.model_provider,
    };
  }
  return {};
}

function readStoredConfig(): StoredAgentConfig {
  const raw = sessionStorage.getItem(STORAGE_KEY);
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? canonicalizeConfig(parsed) : {};
  } catch {
    return {};
  }
}

function writeStoredConfig(config: StoredAgentConfig): StoredAgentConfig | null {
  const normalizedConfig = canonicalizeConfig(config);
  if (!normalizedConfig.model_id && !normalizedConfig.api_key && !normalizedConfig.model_name && !normalizedConfig.api_base && !normalizedConfig.model_provider) {
    sessionStorage.removeItem(STORAGE_KEY);
    return null;
  }
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(normalizedConfig));
  return normalizedConfig;
}

function notifyAgentConfigChanged(): void {
  window.dispatchEvent(new Event(AGENT_CONFIG_CHANGED_EVENT));
}

export function getStoredAgentConfig(): StoredAgentConfig | null {
  const config = readStoredConfig();
  return config.model_id || config.api_key || config.model_name || config.api_base || config.model_provider ? config : null;
}

export function saveStoredAgentConfig(config: StoredAgentConfig): StoredAgentConfig | null {
  const savedConfig = writeStoredConfig(config);
  notifyAgentConfigChanged();
  return savedConfig;
}

export function clearStoredAgentConfig(): void {
  sessionStorage.removeItem(STORAGE_KEY);
  notifyAgentConfigChanged();
}

export function getSavedSelectedModelId(): string {
  return localStorage.getItem(SELECTED_MODEL_STORAGE_KEY) || SYSTEM_MODEL_ID;
}

export function saveSelectedModelId(modelId: string): void {
  if (!modelId || modelId === SYSTEM_MODEL_ID) {
    localStorage.removeItem(SELECTED_MODEL_STORAGE_KEY);
    notifyAgentConfigChanged();
    return;
  }
  localStorage.setItem(SELECTED_MODEL_STORAGE_KEY, modelId);
  notifyAgentConfigChanged();
}

export function buildChatModelOptions(config: ClientConfigResponse | null): ChatModelOption[] {
  const defaultModel = config?.default_model;
  const options: ChatModelOption[] = [
    {
      id: SYSTEM_MODEL_ID,
      label: defaultModel?.label || defaultModel?.model_name || 'System default',
      model_name: defaultModel?.model_name || '',
      model_provider: defaultModel?.model_provider || '',
      api_base: defaultModel?.api_base ?? null,
      is_system_default: true,
    },
  ];

  const seenIds = new Set(options.map((option) => option.id));
  for (const option of config?.available_models || []) {
    if (!seenIds.has(option.id)) {
      options.push(option);
      seenIds.add(option.id);
    }
  }

  const storedConfig = getStoredAgentConfig();
  if (storedConfig && !storedConfig.model_id && storedConfig.model_name) {
    options.push({
      id: CUSTOM_STORED_MODEL_ID,
      label: storedConfig.model_name,
      model_name: storedConfig.model_name,
      model_provider: storedConfig.model_provider || 'custom',
      api_base: storedConfig.api_base ?? null,
      is_custom: true,
    });
  }

  return options;
}

export function ensureSelectedModelId(modelId: string, options: ChatModelOption[]): string {
  if (options.some((option) => option.id === modelId)) {
    return modelId;
  }
  return SYSTEM_MODEL_ID;
}

export function getModelConfigForSelection(modelId: string, options: ChatModelOption[]): StoredAgentConfig | null {
  if (!modelId || modelId === SYSTEM_MODEL_ID) {
    return null;
  }

  const selectedOption = options.find((option) => option.id === modelId);
  if (!selectedOption) {
    return null;
  }

  if (selectedOption.is_custom) {
    return getStoredAgentConfig();
  }

  const storedConfig = getStoredAgentConfig();
  if (storedConfig?.model_id === selectedOption.id) {
    return storedConfig;
  }

  return { model_id: selectedOption.id };
}

export function resolveModelIdForConfig(
  modelConfig: {
    model_id?: string | null;
    model_name?: string | null;
    model_provider?: string | null;
    api_base?: string | null;
  } | null | undefined,
  options: ChatModelOption[],
): string {
  if (!modelConfig) {
    return SYSTEM_MODEL_ID;
  }
  if (modelConfig.model_id && options.some((option) => option.id === modelConfig.model_id)) {
    return modelConfig.model_id;
  }

  const matchedOption = options.find((option) => (
    !option.is_system_default
    && option.model_name === modelConfig.model_name
    && option.model_provider === modelConfig.model_provider
    && (option.api_base || null) === (modelConfig.api_base || null)
  ));
  return matchedOption?.id || CURRENT_SESSION_MODEL_ID;
}

export function upsertCurrentSessionModelOption(
  options: ChatModelOption[],
  modelConfig: {
    model_name?: string | null;
    model_provider?: string | null;
    api_base?: string | null;
  } | null | undefined,
): ChatModelOption[] {
  const nextOptions = options.filter((option) => option.id !== CURRENT_SESSION_MODEL_ID);
  if (!modelConfig?.model_name) {
    return nextOptions;
  }
  return [
    ...nextOptions,
    {
      id: CURRENT_SESSION_MODEL_ID,
      label: modelConfig.model_name,
      model_name: modelConfig.model_name,
      model_provider: modelConfig.model_provider || '',
      api_base: modelConfig.api_base || null,
    },
  ];
}

/**
 * Read and remove a model handoff from the current URL without changing the
 * active model. Authentication code can keep the candidate in memory while it
 * verifies the accompanying access token.
 */
export function captureAgentConfigFromUrl(): CapturedAgentConfigHandoff {
  const searchParams = new URLSearchParams(window.location.search);
  const hashValue = window.location.hash.startsWith('#') ? window.location.hash.slice(1) : window.location.hash;
  const hashParams = new URLSearchParams(hashValue);
  // A URL handoff is one atomic configuration. Never merge a new model id
  // with credentials left behind by a previous model selection.
  const nextConfig: StoredAgentConfig = {};
  let hasAnyConfigParam = false;

  for (const [param, field] of Object.entries(CONFIG_PARAM_MAP)) {
    // Query values are deliberately rejected because they are transmitted to
    // HTTP servers and routinely logged. Legacy handoff is fragment-only.
    const value = hashParams.get(param);
    if (value) {
      nextConfig[field] = value;
    }
    if (searchParams.has(param)) {
      searchParams.delete(param);
      hasAnyConfigParam = true;
    }
    if (hashParams.has(param)) {
      hashParams.delete(param);
      hasAnyConfigParam = true;
    }
  }

  if (hasAnyConfigParam) {
    const search = searchParams.toString();
    const hash = hashParams.toString();
    const cleanUrl = `${window.location.pathname}${search ? `?${search}` : ''}${hash ? `#${hash}` : ''}`;
    window.history.replaceState(window.history.state, document.title, cleanUrl);
  }

  const hasCompleteCustomConfig = Boolean(
    nextConfig.api_key
    && nextConfig.api_base
    && nextConfig.model_name
    && nextConfig.model_provider
  );
  // Backend selection modes are deliberately unambiguous. A complete BYOK
  // tuple is custom and drops model_id; otherwise a catalog selection keeps
  // only model_id and never mixes in partial credentials.
  const acceptedConfig: StoredAgentConfig | null = hasCompleteCustomConfig
    ? {
        api_key: nextConfig.api_key,
        api_base: nextConfig.api_base,
        model_name: nextConfig.model_name,
        model_provider: nextConfig.model_provider,
      }
    : nextConfig.model_id
      ? { model_id: nextConfig.model_id }
      : null;

  return { hasConfigParams: hasAnyConfigParam, config: acceptedConfig };
}

/**
 * Commit a previously captured model handoff. A fresh account handoff without
 * a model explicitly resets account-scoped model state instead of inheriting
 * credentials from the previous account.
 */
export function commitAgentConfigHandoff(
  captured: CapturedAgentConfigHandoff,
  resetWhenAbsent: boolean = true,
): void {
  if (!captured.hasConfigParams && !resetWhenAbsent) return;

  const previousConfig = sessionStorage.getItem(STORAGE_KEY);
  const previousSelectedModel = localStorage.getItem(SELECTED_MODEL_STORAGE_KEY);
  try {
    const savedConfig = captured.config
      ? writeStoredConfig(captured.config)
      : writeStoredConfig({});
    if (savedConfig?.model_id) {
      saveSelectedModelId(savedConfig.model_id);
    } else if (savedConfig?.model_name) {
      saveSelectedModelId(CUSTOM_STORED_MODEL_ID);
    } else {
      saveSelectedModelId(SYSTEM_MODEL_ID);
    }
  } catch (error) {
    if (previousConfig === null) sessionStorage.removeItem(STORAGE_KEY);
    else sessionStorage.setItem(STORAGE_KEY, previousConfig);
    if (previousSelectedModel === null) localStorage.removeItem(SELECTED_MODEL_STORAGE_KEY);
    else localStorage.setItem(SELECTED_MODEL_STORAGE_KEY, previousSelectedModel);
    throw error;
  }
}

export function hydrateAgentConfigFromUrl(allowImport: boolean = false): boolean {
  const captured = captureAgentConfigFromUrl();
  if (allowImport && captured.hasConfigParams) {
    commitAgentConfigHandoff(captured, false);
  }
  return captured.hasConfigParams;
}
