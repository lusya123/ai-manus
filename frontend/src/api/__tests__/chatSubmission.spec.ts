import { beforeEach, describe, expect, it, vi } from 'vitest';

const { createSSEConnection } = vi.hoisted(() => ({
  createSSEConnection: vi.fn(
    async (
      _endpoint: string,
      _options: { body?: Record<string, unknown>; terminalEvents?: readonly string[] },
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
    expect(options.terminalEvents).toEqual(['done', 'error', 'wait']);
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

  it('reconnects one durable turn without resubmitting its message', async () => {
    const submissionId = '22222222-2222-4222-8222-222222222222';
    await chatWithSession(
      'session-1',
      '',
      '1782506372223-0',
      [],
      undefined,
      submissionId,
    );

    const options = createSSEConnection.mock.calls[0]![1];
    expect(options.body).toMatchObject({
      message: '',
      event_id: '1782506372223-0',
      submission_id: submissionId,
    });
  });

  it('creates an idempotency key for a files-only submission', async () => {
    const randomUUID = vi
      .spyOn(globalThis.crypto, 'randomUUID')
      .mockReturnValue('33333333-3333-4333-8333-333333333333');

    await chatWithSession(
      'session-1',
      '',
      undefined,
      [{ file_id: 'file-1', filename: 'report.pdf' }],
    );

    expect(createSSEConnection.mock.calls[0]![1].body).toMatchObject({
      message: '',
      submission_id: '33333333-3333-4333-8333-333333333333',
      attachments: [{ file_id: 'file-1', filename: 'report.pdf' }],
    });
    randomUUID.mockRestore();
  });
});
