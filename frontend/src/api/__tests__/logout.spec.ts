import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '../client';
import { logout } from '../auth';

describe('logout request', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('sends the refresh token and never invokes the 401 refresh interceptor', async () => {
    const post = vi.spyOn(apiClient, 'post').mockResolvedValue({
      data: { code: 0, msg: 'ok', data: {} },
    } as never);

    await expect(logout({ refresh_token: 'paired-refresh-token' })).resolves.toEqual({});

    expect(post).toHaveBeenCalledOnce();
    expect(post).toHaveBeenCalledWith(
      '/auth/logout',
      { refresh_token: 'paired-refresh-token' },
      expect.objectContaining({ __skipAuthRefresh: true }),
    );
  });
});
