import { beforeEach, describe, expect, it, vi } from 'vitest';

const { createSSEConnection } = vi.hoisted(() => ({
  createSSEConnection: vi.fn(
    async (
      _endpoint: string,
      _options: { body?: Record<string, unknown> },
      _callbacks?: unknown,
    ) => vi.fn(),
  ),
}));

vi.mock('../client', () => ({
  API_CONFIG: { host: '' },
  apiClient: {},
  createSSEConnection,
}));

import { chatWithSession } from '../agent';

describe('chat submission idempotency key', () => {
  beforeEach(() => {
    createSSEConnection.mockClear();
  });

  it('freezes one generated UUID into the body reused by SSE retries', async () => {
    const randomUUID = vi
      .spyOn(globalThis.crypto, 'randomUUID')
      .mockReturnValue('11111111-1111-4111-8111-111111111111');

    await chatWithSession('session-1', 'hello');

    expect(randomUUID).toHaveBeenCalledOnce();
    expect(createSSEConnection).toHaveBeenCalledOnce();
    const options = createSSEConnection.mock.calls[0]![1];
    expect(options.body?.submission_id).toBe(
      '11111111-1111-4111-8111-111111111111',
    );
    // The retry closure receives one object; no retry path regenerates it.
    expect(options.body).toBe(createSSEConnection.mock.calls[0]![1].body);
    randomUUID.mockRestore();
  });

  it('omits submission_id for reconnect-only requests', async () => {
    await chatWithSession('session-1', '');
    expect(
      createSSEConnection.mock.calls[0]![1].body?.submission_id,
    ).toBeUndefined();
  });
});
