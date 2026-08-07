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
  onclose: (() => void) | null = null;
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

describe('Claw WebSocket', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('uses the shared cookie-authenticated endpoint without credentials in the URL', () => {
    const onOpen = vi.fn();
    const client = new ClawWebSocket({ onEvent: vi.fn(), onOpen });
    const socket = FakeWebSocket.instances[0];

    expect(socket.url).toContain('/ws/claw');
    expect(socket.url).not.toContain('?token=');

    socket.onopen?.();
    expect(onOpen).toHaveBeenCalledOnce();
    expect(client.isConnected).toBe(true);
    expect(socket.sent).toHaveLength(0);
    client.disconnect();
  });

  it('sends chat frames with optional attachments', () => {
    const client = new ClawWebSocket({ onEvent: vi.fn() });
    const socket = FakeWebSocket.instances[0];

    client.send('hello', 'default', ['file-1']);

    expect(JSON.parse(socket.sent[0])).toEqual({
      type: 'chat',
      message: 'hello',
      session_id: 'default',
      file_ids: ['file-1'],
    });
    client.disconnect();
  });

  it('forwards application events and ignores heartbeats', () => {
    const onEvent = vi.fn();
    const client = new ClawWebSocket({ onEvent });
    const socket = FakeWebSocket.instances[0];

    socket.onmessage?.({
      data: JSON.stringify({ type: 'heartbeat' }),
    } as MessageEvent);
    socket.onmessage?.({
      data: JSON.stringify({ type: 'text', content: 'answer' }),
    } as MessageEvent);

    expect(onEvent).toHaveBeenCalledOnce();
    expect(onEvent).toHaveBeenCalledWith({
      type: 'text',
      content: 'answer',
    });
    client.disconnect();
  });

  it('reconnects after an unexpected close', async () => {
    vi.useFakeTimers();
    const onClose = vi.fn();
    const client = new ClawWebSocket({ onEvent: vi.fn(), onClose });
    const socket = FakeWebSocket.instances[0];

    socket.onclose?.();
    await vi.advanceTimersByTimeAsync(1000);

    expect(onClose).toHaveBeenCalledOnce();
    expect(FakeWebSocket.instances).toHaveLength(2);
    client.disconnect();
  });

  it('does not reconnect after an explicit disconnect', async () => {
    vi.useFakeTimers();
    const client = new ClawWebSocket({ onEvent: vi.fn() });
    const socket = FakeWebSocket.instances[0];

    client.disconnect();
    socket.onclose?.();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
