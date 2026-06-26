export interface StoredAgentConfig {
  api_key?: string;
  api_base?: string;
  model_name?: string;
  model_provider?: string;
}

const STORAGE_KEY = 'sub2api_agent_config';

const CONFIG_PARAM_MAP: Record<string, keyof StoredAgentConfig> = {
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
  if (!normalizedConfig.api_key && !normalizedConfig.model_name && !normalizedConfig.api_base && !normalizedConfig.model_provider) {
    sessionStorage.removeItem(STORAGE_KEY);
    return null;
  }
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(normalizedConfig));
  return normalizedConfig;
}

export function getStoredAgentConfig(): StoredAgentConfig | null {
  const config = readStoredConfig();
  return config.api_key || config.model_name || config.api_base || config.model_provider ? config : null;
}

export function saveStoredAgentConfig(config: StoredAgentConfig): StoredAgentConfig | null {
  return writeStoredConfig(config);
}

export function clearStoredAgentConfig(): void {
  sessionStorage.removeItem(STORAGE_KEY);
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
