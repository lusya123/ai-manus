import type { ClientConfigResponse, ModelOptionResponse } from './config';

export interface StoredAgentConfig {
  model_id?: string;
  api_key?: string;
  api_base?: string;
  model_name?: string;
  model_provider?: string;
}

export interface ChatModelOption extends ModelOptionResponse {
  is_system_default?: boolean;
  is_custom?: boolean;
}

const STORAGE_KEY = 'sub2api_agent_config';
const SELECTED_MODEL_STORAGE_KEY = 'manus_selected_model_id';
export const SYSTEM_MODEL_ID = 'system-default';
export const CURRENT_SESSION_MODEL_ID = 'current-session-model';

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

function readStoredConfig(): StoredAgentConfig {
  const raw = sessionStorage.getItem(STORAGE_KEY);
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? normalizeConfig(parsed) : {};
  } catch {
    return {};
  }
}

function writeStoredConfig(config: StoredAgentConfig): StoredAgentConfig | null {
  const normalizedConfig = normalizeConfig(config);
  if (!normalizedConfig.model_id && !normalizedConfig.api_key && !normalizedConfig.model_name && !normalizedConfig.api_base && !normalizedConfig.model_provider) {
    sessionStorage.removeItem(STORAGE_KEY);
    return null;
  }
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(normalizedConfig));
  return normalizedConfig;
}

export function getStoredAgentConfig(): StoredAgentConfig | null {
  const config = readStoredConfig();
  return config.model_id || config.api_key || config.model_name || config.api_base || config.model_provider ? config : null;
}

export function saveStoredAgentConfig(config: StoredAgentConfig): StoredAgentConfig | null {
  return writeStoredConfig(config);
}

export function clearStoredAgentConfig(): void {
  sessionStorage.removeItem(STORAGE_KEY);
}

export function getSavedSelectedModelId(): string {
  return localStorage.getItem(SELECTED_MODEL_STORAGE_KEY) || SYSTEM_MODEL_ID;
}

export function saveSelectedModelId(modelId: string): void {
  if (!modelId || modelId === SYSTEM_MODEL_ID) {
    localStorage.removeItem(SELECTED_MODEL_STORAGE_KEY);
    return;
  }
  localStorage.setItem(SELECTED_MODEL_STORAGE_KEY, modelId);
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
    ...(config?.available_models || []),
  ];

  const storedConfig = getStoredAgentConfig();
  if (storedConfig && !storedConfig.model_id && storedConfig.model_name) {
    options.push({
      id: 'custom-stored-model',
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

export function hydrateAgentConfigFromUrl(): boolean {
  const searchParams = new URLSearchParams(window.location.search);
  const hashValue = window.location.hash.startsWith('#') ? window.location.hash.slice(1) : window.location.hash;
  const hashParams = new URLSearchParams(hashValue);
  const nextConfig: StoredAgentConfig = { ...readStoredConfig() };
  let changed = false;

  for (const [param, field] of Object.entries(CONFIG_PARAM_MAP)) {
    const value = searchParams.get(param) || hashParams.get(param);
    if (value) {
      nextConfig[field] = value;
      changed = true;
    }
    if (searchParams.has(param)) searchParams.delete(param);
    if (hashParams.has(param)) hashParams.delete(param);
  }

  if (changed) {
    writeStoredConfig(nextConfig);
  }

  return changed;
}
