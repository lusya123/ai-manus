import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { AxiosAdapter, InternalAxiosRequestConfig } from 'axios';
import { createPreviewUrl } from '../agent';
import { apiClient } from '../client';

describe('preview API authentication mode', () => {
  const originalAdapter = apiClient.defaults.adapter;
  let capturedConfig: InternalAxiosRequestConfig | undefined;

  beforeEach(() => {
    localStorage.clear();
    capturedConfig = undefined;
    apiClient.defaults.adapter = (async (config) => {
      capturedConfig = config;
      return {
        data: {
          code: 0,
          msg: 'ok',
          data: { signed_url: '/preview', expires_in: 900 },
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      };
    }) as AxiosAdapter;
  });

  afterEach(() => {
    apiClient.defaults.adapter = originalAdapter;
    delete apiClient.defaults.headers.Authorization;
    localStorage.clear();
  });

  it('removes even a default Authorization header for shared preview access', async () => {
    localStorage.setItem('access_token', 'owner-token');
    apiClient.defaults.headers.Authorization = 'Bearer refreshed-owner-token';

    await createPreviewUrl('shared-session', 'http://localhost:3000', { publicAccess: true });

    expect(capturedConfig?.headers.get('Authorization')).toBeUndefined();
  });

  it('keeps owner auth for a private preview request', async () => {
    localStorage.setItem('access_token', 'owner-token');

    await createPreviewUrl('owned-session', 'http://localhost:3000');

    expect(capturedConfig?.headers.get('Authorization')).toBe('Bearer owner-token');
  });
});
