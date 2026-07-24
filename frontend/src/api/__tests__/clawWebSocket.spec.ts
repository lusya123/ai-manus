import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ClawWebSocket } from '../claw';

class FakeWebSocket {
  static OPEN = 1;
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  readyState = FakeWebSocket.OPEN;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string | URL) {
    this.url = String(url);
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = 3;
  }
}

const closeEvent = (code: number, reason = '') => ({
  code,
  reason,
  wasClean: true,
} as CloseEvent);

const flushPromises = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('Claw WebSocket authentication', () => {
  beforeEach(() => {
    localStorage.clear();
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('keeps the primary bearer token out of the URL and sends it in the first frame', () => {
    localStorage.setItem('access_token', 'primary-secret-token');
    const client = new ClawWebSocket({ onEvent: vi.fn() });
    const socket = FakeWebSocket.instances[0];

    expect(socket.url).not.toContain('primary-secret-token');
    expect(socket.url).not.toContain('?token=');

    socket.onopen?.();

    expect(JSON.parse(socket.sent[0])).toEqual({
      type: 'auth',
      token: 'primary-secret-token',
    });
    client.disconnect();
  });

  it('does not report or use the socket as connected until auth_ack', () => {
    localStorage.setItem('access_token', 'primary-secret-token');
    const onOpen = vi.fn();
    const onEvent = vi.fn();
    const client = new ClawWebSocket({ onOpen, onEvent });
    const socket = FakeWebSocket.instances[0];

    socket.onopen?.();
    client.send('must not be sent yet');

    expect(client.isConnected).toBe(false);
    expect(onOpen).not.toHaveBeenCalled();
    expect(socket.sent).toHaveLength(1);

    socket.onmessage?.({
      data: JSON.stringify({ type: 'text', content: 'pre-auth data' }),
    } as MessageEvent);
    expect(onEvent).not.toHaveBeenCalled();

    socket.onmessage?.({
      data: JSON.stringify({ type: 'auth_ack' }),
    } as MessageEvent);

    expect(client.isConnected).toBe(true);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onEvent).not.toHaveBeenCalled();

    client.send('allowed now');
    expect(JSON.parse(socket.sent[1])).toEqual({
      type: 'chat',
      message: 'allowed now',
      session_id: 'default',
    });
    client.disconnect();
  });

  it('refreshes once on natural expiry and reconnects immediately with the new token', async () => {
    localStorage.setItem('access_token', 'expired-token');
    const refresh = vi.fn(async () => {
      localStorage.setItem('access_token', 'refreshed-token');
      return 'refreshed-token';
    });
    const client = new ClawWebSocket({ onEvent: vi.fn() }, refresh);
    const expiredSocket = FakeWebSocket.instances[0];

    expiredSocket.onclose?.(closeEvent(4002, 'Authentication expired'));
    // A duplicate/stale close notification must share or skip the refresh.
    expiredSocket.onclose?.(closeEvent(4002, 'Authentication expired'));
    await flushPromises();

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    const refreshedSocket = FakeWebSocket.instances[1];
    refreshedSocket.onopen?.();
    expect(JSON.parse(refreshedSocket.sent[0])).toEqual({
      type: 'auth',
      token: 'refreshed-token',
    });
    client.disconnect();
  });

  it('does not refresh or reconnect revoked and invalid credentials', async () => {
    vi.useFakeTimers();
    localStorage.setItem('access_token', 'revoked-token');
    const refresh = vi.fn<() => Promise<string | null>>();
    const client = new ClawWebSocket({ onEvent: vi.fn() }, refresh);
    const socket = FakeWebSocket.instances[0];

    socket.onclose?.(closeEvent(4001, 'Unauthorized'));
    await vi.advanceTimersByTimeAsync(60_000);

    expect(refresh).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(1);
    client.disconnect();
  });

  it('stops reconnecting when an expiry refresh fails', async () => {
    vi.useFakeTimers();
    const refresh = vi.fn().mockRejectedValue(new Error('refresh rejected'));
    const client = new ClawWebSocket({ onEvent: vi.fn() }, refresh);
    const socket = FakeWebSocket.instances[0];

    socket.onclose?.(closeEvent(4002, 'Authentication expired'));
    await vi.runAllTimersAsync();

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances).toHaveLength(1);
    client.disconnect();
  });

  it('does not reconnect after an explicit disconnect during refresh', async () => {
    let finishRefresh: ((token: string) => void) | undefined;
    const refresh = vi.fn(() => new Promise<string>((resolve) => {
      finishRefresh = resolve;
    }));
    const client = new ClawWebSocket({ onEvent: vi.fn() }, refresh);
    const socket = FakeWebSocket.instances[0];

    socket.onclose?.(closeEvent(4002, 'Authentication expired'));
    client.disconnect();
    localStorage.setItem('access_token', 'too-late-token');
    finishRefresh?.('too-late-token');
    await flushPromises();

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
