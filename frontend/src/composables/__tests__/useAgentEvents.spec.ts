import { describe, expect, it, vi } from 'vitest';
import { ref } from 'vue';
import {
  getLatestTurnId,
  hasTerminalEventForLatestTurn,
  hasTerminalEventForTurn,
  useAgentEvents,
} from '../useAgentEvents';
import type { Message, ToolContent } from '../../types/message';
import type { AgentEvent, PlanEventData } from '../../types/event';

const createHarness = () => {
  const state = {
    messages: ref<Message[]>([]),
    title: ref('New Chat'),
    plan: ref<PlanEventData>(),
    lastEventId: ref<string>(),
    lastTool: ref<ToolContent>(),
    lastNoMessageTool: ref<ToolContent>(),
  };
  const onToolActivity = vi.fn();
  const onStreamError = vi.fn();
  return {
    state,
    onToolActivity,
    onStreamError,
    ...useAgentEvents(state, { onToolActivity, onStreamError }),
  };
};

describe('useAgentEvents timeline projection', () => {
  it.each(['done', 'wait'] as const)('records the %s cursor without owning session phase', (event) => {
    const { state, handleEvent } = createHarness();
    expect(handleEvent({
      event,
      data: { event_id: `${event}-id`, timestamp: 1 },
    } as AgentEvent)).toBe(true);
    expect(state.lastEventId.value).toBe(`${event}-id`);
    expect(state.messages.value).toEqual([]);
  });

  it('uses only the Redis transport cursor for reconnect state', () => {
    const { state, handleEvent } = createHarness();
    handleEvent({
      event: 'done',
      data: {
        event_id: 'stable-logical-event',
        transport_cursor: '1782506372223-0',
      },
    } as AgentEvent);
    expect(state.lastEventId.value).toBe('1782506372223-0');
  });

  it('deduplicates Mongo replay and Redis live overlap by stable event id', () => {
    const { state, handleEvent } = createHarness();
    const event = {
      event: 'message',
      data: {
        event_id: 'stable-message',
        role: 'assistant',
        content: 'hello',
        attachments: [],
        timestamp: 1,
      },
    } as AgentEvent;
    expect(handleEvent(event)).toBe(true);
    expect(handleEvent({
      ...event,
      data: { ...event.data, transport_cursor: '1-0' },
    } as AgentEvent)).toBe(false);
    expect(state.messages.value).toHaveLength(1);
    expect(state.lastEventId.value).toBe('1-0');
  });

  it('advances the transport cursor when a terminal event is duplicated', () => {
    const { state, handleEvent } = createHarness();
    const terminal = {
      event: 'done',
      data: { event_id: 'stable-done', transport_cursor: '1-0', timestamp: 1 },
    } as AgentEvent;

    expect(handleEvent(terminal)).toBe(true);
    expect(handleEvent({
      ...terminal,
      data: { ...terminal.data, transport_cursor: '2-0' },
    } as AgentEvent)).toBe(false);

    expect(state.lastEventId.value).toBe('2-0');
    expect(state.messages.value).toEqual([]);
  });

  it('does not duplicate an error message when a terminal error is replayed', () => {
    const { state, handleEvent, onStreamError } = createHarness();
    const terminal = {
      event: 'error',
      data: {
        event_id: 'stable-error',
        error: 'failed safely',
        timestamp: 1,
      },
    } as AgentEvent;

    handleEvent(terminal);
    handleEvent(terminal);

    expect(state.messages.value).toHaveLength(1);
    expect(onStreamError).toHaveBeenCalledOnce();
  });

  it('resets logical event deduplication when the page changes sessions', () => {
    const { state, handleEvent, resetEventHistory } = createHarness();
    const event = {
      event: 'message',
      data: {
        event_id: 'stable-message',
        role: 'assistant',
        content: 'hello',
        attachments: [],
        timestamp: 1,
      },
    } as AgentEvent;

    handleEvent(event);
    resetEventHistory();
    state.messages.value = [];
    handleEvent(event);

    expect(state.messages.value).toHaveLength(1);
  });

  it('ignores terminal events from an older turn when the newest turn is running', () => {
    const events = [
      { event: 'message', data: { role: 'user', event_id: 'u1' } },
      { event: 'done', data: { event_id: 'd1' } },
      { event: 'message', data: { role: 'user', event_id: 'u2' } },
      { event: 'step', data: { event_id: 's2' } },
    ] as AgentEvent[];

    expect(hasTerminalEventForLatestTurn(events)).toBe(false);
  });

  it('reconnects an active session when history has no user turn to identify', () => {
    const events = [
      { event: 'done', data: { event_id: 'orphan-terminal' } },
    ] as AgentEvent[];

    expect(hasTerminalEventForLatestTurn(events)).toBe(false);
  });

  it('detects a terminal event in the newest turn even when metadata follows it', () => {
    const events = [
      { event: 'done', data: { event_id: 'd1' } },
      { event: 'message', data: { role: 'user', event_id: 'u2' } },
      { event: 'done', data: { event_id: 'd2' } },
      { event: 'title', data: { event_id: 'title2' } },
    ] as AgentEvent[];

    expect(hasTerminalEventForLatestTurn(events)).toBe(true);
  });

  it('does not let a late older terminal event complete a newer durable turn', () => {
    const events = [
      { event: 'message', data: { role: 'user', event_id: 'u1', turn_id: 'turn-1' } },
      { event: 'message', data: { role: 'user', event_id: 'u2', turn_id: 'turn-2' } },
      { event: 'done', data: { event_id: 'd1', turn_id: 'turn-1' } },
    ] as AgentEvent[];

    expect(hasTerminalEventForLatestTurn(events)).toBe(false);
  });

  it('matches a terminal event to the newest durable turn by turn id', () => {
    const events = [
      { event: 'message', data: { role: 'user', event_id: 'u1', turn_id: 'turn-1' } },
      { event: 'message', data: { role: 'user', event_id: 'u2', turn_id: 'turn-2' } },
      { event: 'done', data: { event_id: 'd1', turn_id: 'turn-1' } },
      { event: 'done', data: { event_id: 'd2', turn_id: 'turn-2' } },
    ] as AgentEvent[];

    expect(hasTerminalEventForLatestTurn(events)).toBe(true);
    expect(getLatestTurnId(events)).toBe('turn-2');
    expect(hasTerminalEventForTurn(events, 'turn-1')).toBe(true);
    expect(hasTerminalEventForTurn(events, 'turn-2')).toBe(true);
  });

  it('keeps tool activity routed through the shared upstream handler', () => {
    const { state, onToolActivity, handleEvent } = createHarness();
    const tool = {
      event_id: 'tool-event',
      tool_call_id: 'call-1',
      name: 'browser',
      function: 'browser_view',
      args: {},
      status: 'calling',
      timestamp: 1,
    };
    handleEvent({ event: 'tool', data: tool } as AgentEvent);
    expect(state.lastNoMessageTool.value?.tool_call_id).toBe('call-1');
    expect(onToolActivity).toHaveBeenCalledOnce();
  });
});
