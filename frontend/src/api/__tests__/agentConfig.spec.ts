import { beforeEach, describe, expect, it } from 'vitest';
import {
  buildChatModelOptions,
  CUSTOM_STORED_MODEL_ID,
  getModelConfigForSelection,
  getSavedSelectedModelId,
  getStoredAgentConfig,
  hydrateAgentConfigFromUrl,
  saveStoredAgentConfig,
  SYSTEM_MODEL_ID,
  upsertCurrentSessionModelOption,
} from '../agentConfig';
import type { ClientConfigResponse } from '../config';

const clientConfig: ClientConfigResponse = {
  auth_provider: 'none',
  registration_enabled: false,
  show_github_button: false,
  github_repository_url: '',
  google_analytics_id: null,
  claw_enabled: false,
  supported_byok_providers: ['openai', 'anthropic', 'deepseek', 'ollama'],
  default_model: {
    id: 'backend-default',
    label: 'Default model',
    model_name: 'default-model',
    model_provider: 'openai',
    api_base: null,
  },
  available_models: [
    {
      id: 'fast-model',
      label: 'Fast model',
      model_name: 'fast',
      model_provider: 'openai',
      api_base: 'https://example.test/v1',
    },
  ],
};

describe('agent model configuration', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  it('builds system and configured model options', () => {
    const options = buildChatModelOptions(clientConfig);
    expect(options.map((option) => option.id)).toEqual([SYSTEM_MODEL_ID, 'fast-model']);
    expect(options[0].model_name).toBe('default-model');
  });

  it('keeps custom credentials in session storage and sends them for selection', () => {
    saveStoredAgentConfig({
      model_name: 'private-model',
      model_provider: 'openai',
      api_base: 'https://private.test/v1',
      api_key: 'session-secret',
    });
    const options = buildChatModelOptions(clientConfig);

    expect(options.some((option) => option.id === CUSTOM_STORED_MODEL_ID)).toBe(true);
    expect(getModelConfigForSelection(CUSTOM_STORED_MODEL_ID, options)).toEqual({
      model_name: 'private-model',
      model_provider: 'openai',
      api_base: 'https://private.test/v1',
      api_key: 'session-secret',
    });
    expect(localStorage.getItem('sub2api_agent_config')).toBeNull();
  });

  it('refuses to persist partial or mixed model configurations', () => {
    expect(saveStoredAgentConfig({
      model_name: 'missing-key-and-base',
      model_provider: 'openai',
    })).toBeNull();
    expect(getStoredAgentConfig()).toBeNull();

    expect(saveStoredAgentConfig({
      model_id: 'fast-model',
      model_name: 'mixed-model',
      model_provider: 'openai',
      api_base: 'https://models.test/v1',
      api_key: 'mixed-key',
    })).toBeNull();
    expect(getStoredAgentConfig()).toBeNull();
  });

  it('hydrates a model handed off in the URL fragment and selects it', () => {
    window.history.replaceState({}, '', '/?keep=yes#tab=models&manus_model_id=fast-model&manus_api_key=url-secret');

    expect(hydrateAgentConfigFromUrl(true)).toBe(true);
    expect(getStoredAgentConfig()).toEqual({ model_id: 'fast-model' });
    expect(getSavedSelectedModelId()).toBe('fast-model');
    expect(window.location.pathname + window.location.search + window.location.hash).toBe('/?keep=yes#tab=models');
  });

  it('discards model credentials handed off in the query string', () => {
    window.history.replaceState({}, '', '/?keep=yes&manus_model_id=fast-model&manus_api_key=query-secret');

    expect(hydrateAgentConfigFromUrl(true)).toBe(true);
    expect(getStoredAgentConfig()).toBeNull();
    expect(getSavedSelectedModelId()).toBe(SYSTEM_MODEL_ID);
    expect(window.location.search).toBe('?keep=yes');
  });

  it('atomically replaces stale credentials when a new model is handed off', () => {
    saveStoredAgentConfig({
      model_id: 'old-model',
      model_name: 'old-name',
      model_provider: 'old-provider',
      api_base: 'https://old.test/v1',
      api_key: 'old-secret',
    });
    window.history.replaceState({}, '', '/#manus_model_id=fast-model');

    expect(hydrateAgentConfigFromUrl(true)).toBe(true);
    expect(getStoredAgentConfig()).toEqual({ model_id: 'fast-model' });
  });

  it('strips but does not import model fragments outside an authenticated handoff', () => {
    window.history.replaceState({}, '', '/#manus_api_key=attacker-key&manus_api_base=https%3A%2F%2Fattacker.test%2Fv1&manus_model=leak&manus_model_provider=openai');

    expect(hydrateAgentConfigFromUrl(false)).toBe(true);
    expect(getStoredAgentConfig()).toBeNull();
    expect(window.location.hash).toBe('');
  });

  it('treats a complete custom tuple as BYOK and drops a conflicting model id', () => {
    window.history.replaceState({}, '', '/#manus_model_id=fast-model&manus_api_key=custom-key&manus_api_base=https%3A%2F%2Fmodels.test%2Fv1&manus_model=custom-model&manus_model_provider=openai');

    expect(hydrateAgentConfigFromUrl(true)).toBe(true);
    expect(getStoredAgentConfig()).toEqual({
      api_key: 'custom-key',
      api_base: 'https://models.test/v1',
      model_name: 'custom-model',
      model_provider: 'openai',
    });
    expect(getSavedSelectedModelId()).toBe(CUSTOM_STORED_MODEL_ID);
  });

  it('replaces the synthetic current-session option when sessions change', () => {
    const first = upsertCurrentSessionModelOption(buildChatModelOptions(clientConfig), {
      model_name: 'legacy-a',
      model_provider: 'openai',
      api_base: null,
    });
    const second = upsertCurrentSessionModelOption(first, {
      model_name: 'legacy-b',
      model_provider: 'anthropic',
      api_base: 'https://second.test',
    });

    expect(second.filter((option) => option.id === 'current-session-model')).toHaveLength(1);
    expect(second.find((option) => option.id === 'current-session-model')?.label).toBe('legacy-b');
  });
});
