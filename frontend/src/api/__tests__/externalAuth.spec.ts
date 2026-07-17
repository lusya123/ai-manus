import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '../client';
import {
  buildSub2ApiLoginUrl,
  captureExternalAuthHandoff,
  clearAuthToken,
  clearStoredTokens,
  completeExternalAuthHandoff,
  getStoredExternalAuthToken,
  getStoredRefreshToken,
  getStoredToken,
  hydrateStoredExternalAuthToken,
  storeToken,
} from '../auth';
import {
  getSavedSelectedModelId,
  getStoredAgentConfig,
  saveStoredAgentConfig,
} from '../agentConfig';

const verifiedUser = {
  id: 'user-1',
  fullname: 'Verified User',
  email: 'verified@example.test',
  role: 'user',
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

function beginHandoff(): string {
  const loginUrl = buildSub2ApiLoginUrl(
    'https://accounts.example.test/login?client=manus',
    'http://localhost/chat',
  );
  const parsed = new URL(loginUrl);
  const state = parsed.searchParams.get('state');
  expect(state).toMatch(/^[a-f0-9]{64,}$/);
  const callback = new URL(parsed.searchParams.get('redirect_uri') as string);
  expect(callback.origin + callback.pathname).toBe('http://localhost/chat');
  expect(callback.searchParams.get('state')).toBe(state);
  return state as string;
}

function acceptCurrentUser(token: string = 'access-secret') {
  return vi.spyOn(apiClient, 'get').mockResolvedValue({
    data: { code: 0, msg: 'ok', data: verifiedUser },
  } as never).mockName(`accept-${token}`);
}

describe('Sub2API auth handoff', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearAuthToken();
    window.history.replaceState({}, '', '/');
  });

  it('creates a high-entropy, per-attempt state in session storage and login URL', () => {
    const first = beginHandoff();
    const second = beginHandoff();

    expect(first).not.toBe(second);
    expect(sessionStorage.getItem('sub2api_external_auth_state')).toBe(second);
  });

  it.each([
    ['missing', ''],
    ['wrong', '&state=attacker-state'],
  ])('rejects a callback with %s state without changing auth or model state', (_, statePart) => {
    beginHandoff();
    saveStoredAgentConfig({ model_id: 'victim-model' });
    window.history.replaceState(
      { preserved: true },
      '',
      `/chat?keep=yes#tab=files&manus_access_token=attacker-token&refresh_token=attacker-refresh&manus_model_id=attacker-model${statePart}`,
    );

    expect(captureExternalAuthHandoff()).toBeNull();
    expect(getStoredToken()).toBeNull();
    expect(getStoredRefreshToken()).toBeNull();
    expect(getStoredAgentConfig()).toEqual({ model_id: 'victim-model' });
    expect(window.location.pathname + window.location.search + window.location.hash).toBe('/chat?keep=yes#tab=files');
    expect(window.history.state).toEqual({ preserved: true });
  });

  it('consumes a matching state before verification and rejects replay', () => {
    const state = beginHandoff();
    const callback = `/#manus_access_token=one-time-token&state=${state}`;
    window.history.replaceState({}, '', callback);

    expect(captureExternalAuthHandoff()).not.toBeNull();
    expect(sessionStorage.getItem('sub2api_external_auth_state')).toBeNull();

    window.history.replaceState({}, '', callback);
    expect(captureExternalAuthHandoff()).toBeNull();
    expect(getStoredToken()).toBeNull();
  });

  it('rejects an unsolicited account replacement when already authenticated', () => {
    storeToken('victim-token');
    saveStoredAgentConfig({ model_id: 'victim-model' });
    const state = beginHandoff();
    const callback = `/#manus_access_token=attacker-token&manus_model_id=attacker-model&state=${state}`;
    window.history.replaceState({}, '', callback);

    expect(captureExternalAuthHandoff()).toBeNull();
    expect(getStoredToken()).toBe('victim-token');
    expect(getStoredAgentConfig()).toEqual({ model_id: 'victim-model' });

    // The matching state was consumed even though account replacement was
    // rejected, so logging out cannot make the attacker link replayable.
    localStorage.removeItem('access_token');
    window.history.replaceState({}, '', callback);
    expect(captureExternalAuthHandoff()).toBeNull();
  });

  it('verifies a token before atomically importing auth and model state', async () => {
    const state = beginHandoff();
    window.history.replaceState(
      {},
      '',
      `/?state=${state}#manus_access_token=access-secret&refresh_token=refresh-secret`
        + '&manus_api_key=model-secret&manus_api_base=https%3A%2F%2Fmodels.example.test%2Fv1'
        + '&manus_model=private-model&manus_model_provider=openai',
    );
    const handoff = captureExternalAuthHandoff();
    expect(handoff).not.toBeNull();
    expect(window.location.hash).toBe('');

    let resolveVerification!: (value: unknown) => void;
    const verification = new Promise((resolve) => {
      resolveVerification = resolve;
    });
    const getSpy = vi.spyOn(apiClient, 'get').mockReturnValue(verification as never);
    const completion = completeExternalAuthHandoff(handoff!);

    // Nothing is observable while /auth/me is unresolved.
    expect(getStoredToken()).toBeNull();
    expect(getStoredRefreshToken()).toBeNull();
    expect(getStoredAgentConfig()).toBeNull();

    resolveVerification({ data: { code: 0, msg: 'ok', data: verifiedUser } });
    await expect(completion).resolves.toBe(true);

    expect(getSpy).toHaveBeenCalledWith('/auth/me', expect.objectContaining({
      headers: { Authorization: 'Bearer access-secret' },
      __skipAuthRefresh: true,
    }));
    expect(getStoredToken()).toBe('access-secret');
    expect(getStoredExternalAuthToken()).toBe('access-secret');
    expect(getStoredRefreshToken()).toBe('refresh-secret');
    expect(getStoredAgentConfig()).toEqual({
      api_key: 'model-secret',
      api_base: 'https://models.example.test/v1',
      model_name: 'private-model',
      model_provider: 'openai',
    });
    expect(getSavedSelectedModelId()).toBe('custom-stored-model');

    await expect(completeExternalAuthHandoff(handoff!)).resolves.toBe(false);
    expect(getSpy).toHaveBeenCalledTimes(1);
  });

  it('commits neither credentials nor model state when /auth/me rejects the token', async () => {
    const state = beginHandoff();
    window.history.replaceState(
      {},
      '',
      `/#manus_access_token=invalid-token&refresh_token=invalid-refresh&manus_model_id=attacker-model&state=${state}`,
    );
    const handoff = captureExternalAuthHandoff();
    vi.spyOn(apiClient, 'get').mockRejectedValue(new Error('Unauthorized'));

    await expect(completeExternalAuthHandoff(handoff!)).resolves.toBe(false);
    expect(getStoredToken()).toBeNull();
    expect(getStoredExternalAuthToken()).toBeNull();
    expect(getStoredRefreshToken()).toBeNull();
    expect(getStoredAgentConfig()).toBeNull();
    expect(sessionStorage.getItem('sub2api_external_auth_state')).toBeNull();
  });

  it('discards query credentials without importing them', () => {
    const state = beginHandoff();
    window.history.replaceState(
      {},
      '',
      `/chat?keep=yes&manus_access_token=query-secret&refresh_token=query-refresh&state=${state}#tab=files`,
    );

    expect(captureExternalAuthHandoff()).toBeNull();
    expect(getStoredToken()).toBeNull();
    expect(getStoredRefreshToken()).toBeNull();
    expect(window.location.pathname + window.location.search + window.location.hash).toBe('/chat?keep=yes#tab=files');
  });

  it('validates a same-origin host token before importing it', async () => {
    localStorage.setItem('auth_token', 'host-token');
    const getSpy = acceptCurrentUser('host-token');

    await expect(hydrateStoredExternalAuthToken()).resolves.toBe(true);
    expect(getSpy).toHaveBeenCalledWith('/auth/me', expect.objectContaining({
      headers: { Authorization: 'Bearer host-token' },
    }));
    expect(getStoredToken()).toBe('host-token');
    expect(localStorage.getItem('sub2api_auth_token')).toBe('host-token');
  });

  it('does not immediately re-import a host token after local logout', async () => {
    localStorage.setItem('auth_token', 'host-token');
    acceptCurrentUser('host-token');
    await expect(hydrateStoredExternalAuthToken()).resolves.toBe(true);

    clearStoredTokens();

    await expect(hydrateStoredExternalAuthToken()).resolves.toBe(false);
    expect(getStoredToken()).toBeNull();
    expect(localStorage.getItem('auth_token')).toBe('host-token');
  });
});
