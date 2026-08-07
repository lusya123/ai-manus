import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatWebSocket } from '../chatWs';

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;

  static instances: FakeWebSocket[] = [];
  static chatFrames: Array<Record<string, unknown>> = [];

  readyState = FakeWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(_url: string) {
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.();
    });
  }

  send(raw: string) {
    const frame = JSON.parse(raw) as Record<string, unknown>;
    if (frame.type === 'join_session') {
      queueMicrotask(() => this.receive({
        type: 'joined',
        session_id: frame.session_id,
        request_id: frame.id,
      }));
      return;
    }
    if (frame.type !== 'chat') return;

    FakeWebSocket.chatFrames.push(frame);
    // Simulate the first ACK being lost. The retry is acknowledged with the
    // same durable UUID and the backend's canonical submission id.
    if (FakeWebSocket.chatFrames.length === 2) {
      queueMicrotask(() => this.receive({
        type: 'ack',
        request_id: frame.id,
        submission_id: frame.id,
        op: 'chat',
        session_id: frame.session_id,
        ok: true,
      }));
    }
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  private receive(message: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
}

describe('ChatWebSocket durable submission retry', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    FakeWebSocket.chatFrames = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('reuses one UUID after an ACK timeout and exposes submission_id', async () => {
    const ws = new ChatWebSocket();
    ws.connect();
    await vi.runAllTicks();
    const submissionId = 'bcb13a5e-10b9-49c2-b9eb-c8f633af05c6';

    const ackPromise = ws.chat({
      sessionId: 'session-1',
      message: 'only once',
      submissionId,
    });
    await vi.runAllTimersAsync();
    const ack = await ackPromise;

    expect(FakeWebSocket.chatFrames).toHaveLength(2);
    expect(FakeWebSocket.chatFrames.map(frame => frame.id)).toEqual([
      submissionId,
      submissionId,
    ]);
    expect(ack).toEqual({ requestId: submissionId, submissionId });
    ws.destroy();
  });
});
