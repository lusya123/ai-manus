import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  EventSourceMessage,
  FetchEventSourceInit,
} from '@microsoft/fetch-event-source';

const { fetchEventSourceMock } = vi.hoisted(() => ({
  fetchEventSourceMock: vi.fn(),
}));

vi.mock('@microsoft/fetch-event-source', () => ({
  fetchEventSource: fetchEventSourceMock,
}));

import { apiClient, createSSEConnection } from '../client';

const successfulSSE = () => new Response(null, {
  status: 200,
  headers: { 'Content-Type': 'text/event-stream; charset=utf-8' },
});

const agentEvent = (data: unknown): EventSourceMessage => ({
  id: 'event-1',
  event: 'message',
  data: JSON.stringify(data),
});

const deferred = () => {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

describe('createSSEConnection lifecycle', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    fetchEventSourceMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it('keeps a transient network retry non-terminal until the stream closes', async () => {
    const retryObserved = deferred();
    const continueRetry = deferred();
    let retryDelay: number | null | undefined | void;

    fetchEventSourceMock.mockImplementation(
      async (_input: RequestInfo, init: FetchEventSourceInit) => {
        retryDelay = init.onerror?.(new TypeError('temporary network failure'));
        retryObserved.resolve();
        await continueRetry.promise;
        await init.onopen?.(successfulSSE());
        init.onmessage?.(agentEvent({ content: 'recovered' }));
        init.onclose?.();
      },
    );

    const onOpen = vi.fn();
    const onMessage = vi.fn();
    const onClose = vi.fn();
    const onError = vi.fn();
    const cancel = await createSSEConnection('/sessions/example/chat', {}, {
      onOpen,
      onMessage,
      onClose,
      onError,
    });

    await retryObserved.promise;
    expect(retryDelay).toBe(1_000);
    expect(onError).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    continueRetry.resolve();
    await vi.waitFor(() => expect(onClose).toHaveBeenCalledOnce());

    expect(onOpen).toHaveBeenCalledOnce();
    expect(onMessage).toHaveBeenCalledWith({
      event: 'message',
      data: { content: 'recovered' },
    });
    expect(onError).not.toHaveBeenCalled();
    cancel();
  });

  it('refreshes a 401 inside one lifecycle and receives later events with the new token', async () => {
    localStorage.setItem('access_token', 'expired-access-token');
    localStorage.setItem('refresh_token', 'valid-refresh-token');
    const refreshResponse = {
      data: {
        data: {
          access_token: 'fresh-access-token',
          refresh_token: 'rotated-refresh-token',
        },
      },
    } as unknown as Awaited<ReturnType<typeof apiClient.post>>;
    const refreshRequest = vi.spyOn(apiClient, 'post').mockResolvedValue(refreshResponse);
    const networkFetch = vi.fn().mockResolvedValue(successfulSSE());
    vi.stubGlobal('fetch', networkFetch);

    const authRetryObserved = deferred();
    const continueRetry = deferred();
    fetchEventSourceMock.mockImplementation(
      async (input: RequestInfo, init: FetchEventSourceInit) => {
        let authRetryError: unknown;
        try {
          await init.onopen?.(new Response(null, { status: 401 }));
        } catch (error) {
          authRetryError = error;
        }

        expect(init.onerror?.(authRetryError)).toBe(0);
        authRetryObserved.resolve();
        await continueRetry.promise;

        const response = await init.fetch?.(input, {
          method: init.method,
          headers: init.headers,
        });
        if (!response) throw new Error('Expected the SSE retry fetch to run');
        await init.onopen?.(response);
        init.onmessage?.(agentEvent({ content: 'after refresh' }));
        init.onclose?.();
      },
    );

    const onMessage = vi.fn();
    const onClose = vi.fn();
    const onError = vi.fn();
    const cancel = await createSSEConnection('/sessions/example/chat', {}, {
      onMessage,
      onClose,
      onError,
    });

    await authRetryObserved.promise;
    expect(fetchEventSourceMock).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();

    continueRetry.resolve();
    await vi.waitFor(() => expect(onClose).toHaveBeenCalledOnce());

    expect(refreshRequest).toHaveBeenCalledOnce();
    expect(localStorage.getItem('access_token')).toBe('fresh-access-token');
    expect(localStorage.getItem('refresh_token')).toBe('rotated-refresh-token');
    const retryHeaders = new Headers(networkFetch.mock.calls[0]?.[1]?.headers);
    expect(retryHeaders.get('Authorization')).toBe('Bearer fresh-access-token');
    expect(onMessage).toHaveBeenCalledWith({
      event: 'message',
      data: { content: 'after refresh' },
    });
    expect(onError).not.toHaveBeenCalled();
    cancel();
  });

  it('notifies one fatal error without an early or duplicate close', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    fetchEventSourceMock.mockImplementation(
      async (_input: RequestInfo, init: FetchEventSourceInit) => {
        try {
          await init.onopen?.(new Response(null, { status: 400 }));
        } catch (error) {
          init.onerror?.(error);
        }
      },
    );

    const onClose = vi.fn();
    const onError = vi.fn();
    const cancel = await createSSEConnection('/sessions/example/chat', {}, {
      onClose,
      onError,
    });

    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    expect(onError.mock.calls[0]?.[0]).toMatchObject({
      name: 'FatalSSEError',
      message: 'SSE request failed with HTTP 400',
    });
    expect(onClose).not.toHaveBeenCalled();
    cancel();
  });

  it('aborts without reporting a terminal error or close', async () => {
    const abortObserved = deferred();
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    fetchEventSourceMock.mockImplementation(
      (_input: RequestInfo, init: FetchEventSourceInit) => new Promise<void>((resolve) => {
        init.signal?.addEventListener('abort', () => {
          abortObserved.resolve();
          resolve();
        }, { once: true });
      }),
    );

    const onClose = vi.fn();
    const onError = vi.fn();
    const cancel = await createSSEConnection('/sessions/example/chat', {}, {
      onClose,
      onError,
    });

    cancel();
    await abortObserved.promise;
    await Promise.resolve();

    expect(onError).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(consoleError).not.toHaveBeenCalled();
  });

  it('closes the transport immediately after delivering a terminal event', async () => {
    fetchEventSourceMock.mockImplementation(
      async (_input: RequestInfo, init: FetchEventSourceInit) => {
        await init.onopen?.(successfulSSE());
        init.onmessage?.({
          id: 'done-1',
          event: 'done',
          data: JSON.stringify({ event_id: 'done-1' }),
        });
      },
    );

    const onMessage = vi.fn();
    const onClose = vi.fn();
    const onError = vi.fn();
    const cancel = await createSSEConnection(
      '/sessions/example/chat',
      { terminalEvents: ['done', 'error', 'wait'] },
      { onMessage, onClose, onError },
    );

    await vi.waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(onMessage).toHaveBeenCalledWith({
      event: 'done',
      data: { event_id: 'done-1' },
    });
    expect(onError).not.toHaveBeenCalled();
    cancel();
  });

  it('retries a clean EOF without a terminal event and then reports one final error', async () => {
    fetchEventSourceMock.mockImplementation(
      async (_input: RequestInfo, init: FetchEventSourceInit) => {
        for (let attempt = 0; attempt < 3; attempt += 1) {
          await init.onopen?.(successfulSSE());
          let closeError: unknown;
          try {
            init.onclose?.();
          } catch (error) {
            closeError = error;
          }
          try {
            init.onerror?.(closeError);
          } catch {
            return;
          }
        }
      },
    );

    const onClose = vi.fn();
    const onRetry = vi.fn();
    const onError = vi.fn();
    const cancel = await createSSEConnection(
      '/sessions/example/chat',
      {
        terminalEvents: ['done'],
        maxRetryAttempts: 2,
        maxRetryDurationMs: 1_000,
      },
      { onClose, onRetry, onError },
    );

    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    expect(onRetry.mock.calls.map(([info]) => info.attempt)).toEqual([1, 2]);
    expect(onClose).not.toHaveBeenCalled();
    expect(onError.mock.calls[0]?.[0].message).toContain('retry limit exceeded');
    cancel();
  });

  it('resets the consecutive retry budget only after newly applied progress', async () => {
    fetchEventSourceMock.mockImplementation(
      async (_input: RequestInfo, init: FetchEventSourceInit) => {
        expect(init.onerror?.(new TypeError('first outage'))).toBe(1_000);
        await init.onopen?.(successfulSSE());
        init.onmessage?.(agentEvent({ event_id: 'new-progress' }));

        expect(init.onerror?.(new TypeError('second outage'))).toBe(1_000);
        await init.onopen?.(successfulSSE());
        init.onmessage?.(agentEvent({ event_id: 'replayed-progress' }));

        expect(init.onerror?.(new TypeError('third outage'))).toBe(1_000);
        try {
          init.onerror?.(new TypeError('fourth outage'));
        } catch {
          // Expected: the duplicate replay did not replenish the retry budget.
        }
      },
    );

    const onRetry = vi.fn();
    const onError = vi.fn();
    const cancel = await createSSEConnection(
      '/sessions/example/chat',
      {
        terminalEvents: ['done'],
        maxRetryAttempts: 2,
        maxRetryDurationMs: 1_000,
      },
      {
        onMessage: ({ data }) => (
          (data as { event_id: string }).event_id === 'new-progress'
        ),
        onRetry,
        onError,
      },
    );

    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    expect(onRetry.mock.calls.map(([info]) => info.attempt)).toEqual([1, 1, 2]);
    expect(onError.mock.calls[0]?.[0].message).toContain('retry limit exceeded');
    cancel();
  });

  it('does not treat comment-only ping frames as business progress', async () => {
    fetchEventSourceMock.mockImplementation(
      async (_input: RequestInfo, init: FetchEventSourceInit) => {
        expect(init.onerror?.(new TypeError('first outage'))).toBe(1_000);
        await init.onopen?.(successfulSSE());
        init.onmessage?.({ id: '', event: '', data: '' });
        try {
          init.onerror?.(new TypeError('second outage'));
        } catch {
          // Expected: the empty ping never replenished the retry budget.
        }
      },
    );

    const onMessage = vi.fn();
    const onError = vi.fn();
    const cancel = await createSSEConnection(
      '/sessions/example/chat',
      {
        terminalEvents: ['done'],
        maxRetryAttempts: 1,
        maxRetryDurationMs: 1_000,
      },
      { onMessage, onError },
    );

    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    expect(onMessage).not.toHaveBeenCalled();
    expect(onError.mock.calls[0]?.[0].message).toContain('retry limit exceeded');
    cancel();
  });

  it.each([
    ['HTTP 429', () => new Response(null, { status: 429 })],
    ['HTTP 503', () => new Response(null, { status: 503 })],
    ['network error', () => new TypeError('network unavailable')],
  ])('bounds persistent retryable %s failures', async (_label, failure) => {
    fetchEventSourceMock.mockImplementation(
      async (_input: RequestInfo, init: FetchEventSourceInit) => {
        for (let attempt = 0; attempt < 2; attempt += 1) {
          let retryError: unknown = failure();
          if (retryError instanceof Response) {
            try {
              await init.onopen?.(retryError);
            } catch (error) {
              retryError = error;
            }
          }
          try {
            init.onerror?.(retryError);
          } catch {
            return;
          }
        }
      },
    );

    const onRetry = vi.fn();
    const onError = vi.fn();
    const cancel = await createSSEConnection(
      '/sessions/example/chat',
      {
        maxRetryAttempts: 1,
        maxRetryDurationMs: 1_000,
      },
      { onRetry, onError },
    );

    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    expect(onRetry).toHaveBeenCalledOnce();
    expect(onRetry.mock.calls[0]?.[0]).toMatchObject({
      attempt: 1,
      nextRetryMs: 1_000,
    });
    cancel();
  });

  it('enforces a total retry deadline even if the next fetch never settles', async () => {
    vi.useFakeTimers();
    const pending = deferred();
    fetchEventSourceMock.mockImplementation(
      async (_input: RequestInfo, init: FetchEventSourceInit) => {
        init.onerror?.(new TypeError('temporary outage'));
        await pending.promise;
      },
    );

    const onRetry = vi.fn();
    const onError = vi.fn();
    const cancel = await createSSEConnection(
      '/sessions/example/chat',
      { maxRetryDurationMs: 25 },
      { onRetry, onError },
    );

    expect(onRetry).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(25);
    expect(onError).toHaveBeenCalledOnce();
    expect(onError.mock.calls[0]?.[0].message).toContain('retry deadline exceeded');
    cancel();
    pending.resolve();
  });
});
