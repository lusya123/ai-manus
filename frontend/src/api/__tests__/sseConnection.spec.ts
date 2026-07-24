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
    expect(retryDelay).toBeUndefined();
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
});
