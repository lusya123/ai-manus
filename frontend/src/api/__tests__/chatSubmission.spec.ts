import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentEvent } from '../../types/event';

const { chatWsMocks } = vi.hoisted(() => ({
  chatWsMocks: {
    setHandlers: vi.fn(),
    clearHandlers: vi.fn(),
    chat: vi.fn(),
    joinSession: vi.fn(),
  },
}));

vi.mock('../chatWs', () => ({
  getChatWebSocket: () => chatWsMocks,
  createChatSubmissionId: () => 'c488a71a-7655-476a-9f5e-14dc480070b0',
}));

import { chatWithSession } from '../agent';

describe('chatWithSession WebSocket submission', () => {
  beforeEach(() => {
    Object.values(chatWsMocks).forEach(mock => mock.mockReset());
    chatWsMocks.chat.mockImplementation(async ({ submissionId }) => ({
      requestId: submissionId,
      submissionId,
    }));
    chatWsMocks.joinSession.mockResolvedValue(undefined);
  });

  it('sends one message frame with its cursor and attachments', async () => {
    const attachments = [{ file_id: 'file-1', filename: 'report.pdf' }];

    const cancel = await chatWithSession(
      'session-1',
      'hello',
      '1782506372223-0',
      attachments,
    );

    expect(chatWsMocks.setHandlers).toHaveBeenCalledOnce();
    expect(chatWsMocks.chat).toHaveBeenCalledOnce();
    expect(chatWsMocks.chat).toHaveBeenCalledWith({
      sessionId: 'session-1',
      message: 'hello',
      lastEventId: '1782506372223-0',
      attachments,
      submissionId: expect.stringMatching(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
      ),
    });
    expect(chatWsMocks.joinSession).not.toHaveBeenCalled();

    cancel();
    expect(chatWsMocks.clearHandlers).toHaveBeenCalledWith('session-1');
  });

  it('joins from the transport cursor without resubmitting on reconnect', async () => {
    await chatWithSession(
      'session-1',
      '',
      '1782506372223-0',
      [],
      undefined,
      'legacy-submission-id',
    );

    expect(chatWsMocks.joinSession).toHaveBeenCalledWith(
      'session-1',
      '1782506372223-0',
    );
    expect(chatWsMocks.chat).not.toHaveBeenCalled();
  });

  it('uses a chat frame for a files-only submission', async () => {
    const attachments = [{ file_id: 'file-1', filename: 'report.pdf' }];

    await chatWithSession('session-1', '', undefined, attachments);

    expect(chatWsMocks.chat).toHaveBeenCalledWith({
      sessionId: 'session-1',
      message: '',
      lastEventId: undefined,
      attachments,
      submissionId: expect.any(String),
    });
    expect(chatWsMocks.joinSession).not.toHaveBeenCalled();
  });

  it('reuses a caller-provided durable UUID and exposes the server acknowledgement', async () => {
    const submissionId = 'fce9cb69-0490-4bf8-a107-cb03f1c59983';
    const canonicalSubmissionId = 'e5d4a719-666e-4f1c-8d20-d922713e06f9';
    const onSubmissionAck = vi.fn();
    chatWsMocks.chat.mockResolvedValue({
      requestId: submissionId,
      submissionId: canonicalSubmissionId,
    });

    const cancel = await chatWithSession(
      'session-1',
      'retry this turn',
      undefined,
      undefined,
      { onSubmissionAck },
      submissionId,
    );

    expect(chatWsMocks.chat).toHaveBeenCalledWith(expect.objectContaining({
      submissionId,
    }));
    expect(onSubmissionAck).toHaveBeenCalledWith({
      requestId: submissionId,
      submissionId: canonicalSubmissionId,
    });
    expect(cancel.requestId).toBe(submissionId);
    expect(cancel.submissionId).toBe(canonicalSubmissionId);
  });

  it('routes status and lifecycle frames through their dedicated callbacks', async () => {
    const onMessage = vi.fn();
    const onStatusUpdate = vi.fn();
    const onClose = vi.fn();
    const onError = vi.fn();

    await chatWithSession('session-1', '', undefined, undefined, {
      onMessage,
      onStatusUpdate,
      onClose,
      onError,
    });

    const handlers = chatWsMocks.setHandlers.mock.calls[0]![1];
    handlers.onStatusUpdate('running');
    handlers.onEvent({
      event: 'status_update',
      data: {
        event_id: 'status-1',
        timestamp: 1,
        agent_status: 'running',
      },
    });
    handlers.onEvent({
      event: 'done',
      data: { event_id: 'done-1', timestamp: 2 },
    } satisfies AgentEvent);
    handlers.onStreamEnd();
    handlers.onError('socket failed');

    expect(onStatusUpdate).toHaveBeenCalledWith('running');
    expect(onMessage).toHaveBeenCalledOnce();
    expect(onMessage).toHaveBeenCalledWith({
      event: 'done',
      data: { event_id: 'done-1', timestamp: 2 },
    });
    expect(onClose).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: 'socket failed' }));
  });
});
