import type { Ref } from 'vue';
import {
  Message,
  MessageContent,
  ToolContent,
  StepContent,
  AttachmentsContent,
} from '../types/message';
import {
  StepEventData,
  ToolEventData,
  MessageEventData,
  ErrorEventData,
  TitleEventData,
  PlanEventData,
  AgentEvent,
} from '../types/event';

const TERMINAL_EVENTS = new Set<AgentEvent['event']>(['done', 'error', 'wait']);

export function getLatestTurnId(events: AgentEvent[]): string | undefined {
  let latestTurnId: string | undefined;
  for (const event of events) {
    const role = (event.data as { role?: string }).role;
    if ((event.event === 'message' || event.event === 'attachments') && role === 'user') {
      latestTurnId = event.data.turn_id;
    }
  }
  return latestTurnId;
}

export function hasTerminalEventForTurn(events: AgentEvent[], turnId: string): boolean {
  return events.some(
    (event) => TERMINAL_EVENTS.has(event.event) && event.data.turn_id === turnId,
  );
}

export function hasTerminalEventForLatestTurn(events: AgentEvent[]): boolean {
  let latestTurnStart = -1;
  events.forEach((event, index) => {
    const role = (event.data as { role?: string }).role;
    if ((event.event === 'message' || event.event === 'attachments') && role === 'user') {
      latestTurnStart = index;
    }
  });
  if (latestTurnStart < 0) return false;
  const latestTurnId = getLatestTurnId(events);
  if (latestTurnId) return hasTerminalEventForTurn(events, latestTurnId);
  return events.slice(latestTurnStart).some((event) => TERMINAL_EVENTS.has(event.event));
}

export interface AgentEventState {
  messages: Ref<Message[]>;
  title: Ref<string>;
  plan: Ref<PlanEventData | undefined>;
  lastEventId: Ref<string | undefined>;
  lastTool: Ref<ToolContent | undefined>;
  lastNoMessageTool: Ref<ToolContent | undefined>;
}

export interface AgentEventOptions {
  /** Called when a non-message tool is created or updated, so the page can surface it (e.g. in the tool panel). */
  onToolActivity?: (tool: ToolContent) => void;
  /** Fired when stream shows an error assistant bubble or step failed — page maps to phase. */
  onStreamError?: () => void;
}

/**
 * Shared conversion of agent stream events into the UI message list.
 * Used by both ChatPage (live chat) and SharePage (replay).
 */
export function useAgentEvents(state: AgentEventState, options: AgentEventOptions = {}) {
  const { messages, title, plan, lastEventId, lastTool, lastNoMessageTool } = state;
  const seenEventIds = new Set<string>();

  const resetEventHistory = () => {
    seenEventIds.clear();
  };

  const getLastStep = (): StepContent | undefined => {
    return messages.value.filter(message => message.type === 'step').pop()?.content as StepContent;
  };

  const handleMessageEvent = (messageData: MessageEventData) => {
    // Skip blank assistant bubbles (e.g. empty create_plan.message from LLM)
    const text = (messageData.content ?? '').trim();
    if (messageData.role === 'assistant' && !text) {
      if (messageData.attachments && messageData.attachments.length > 0) {
        messages.value.push({
          type: 'attachments',
          content: {
            ...messageData
          } as AttachmentsContent,
        });
      }
      return;
    }

    // User turn: keep attachments on the same ChatQuestion shell (images above bubble).
    if (messageData.role === 'user') {
      messages.value.push({
        type: 'user',
        content: {
          ...messageData,
          attachments: messageData.attachments?.length ? messageData.attachments : undefined,
        } as MessageContent,
      });
      return;
    }

    messages.value.push({
      type: messageData.role,
      content: {
        ...messageData
      } as MessageContent,
    });

    if (messageData.attachments && messageData.attachments.length > 0) {
      messages.value.push({
        type: 'attachments',
        content: {
          ...messageData
        } as AttachmentsContent,
      });
    }
  };

  const handleToolEvent = (toolData: ToolEventData) => {
    const lastStep = getLastStep();
    const toolContent: ToolContent = {
      ...toolData
    };
    if (lastTool.value && lastTool.value.tool_call_id === toolContent.tool_call_id) {
      Object.assign(lastTool.value, toolContent);
    } else {
      if (lastStep?.status === 'running') {
        lastStep.tools.push(toolContent);
      } else {
        messages.value.push({
          type: 'tool',
          content: toolContent,
        });
      }
      lastTool.value = toolContent;
    }
    if (toolContent.name !== 'message') {
      lastNoMessageTool.value = toolContent;
      options.onToolActivity?.(toolContent);
    }
  };

  const handleStepEvent = (stepData: StepEventData) => {
    const lastStep = getLastStep();
    if (stepData.status === 'running') {
      messages.value.push({
        type: 'step',
        content: {
          ...stepData,
          tools: []
        } as StepContent,
      });
    } else if (stepData.status === 'completed') {
      if (lastStep) {
        lastStep.status = stepData.status;
      }
    } else if (stepData.status === 'failed') {
      options.onStreamError?.();
    }
  };

  const handleErrorEvent = (errorData: ErrorEventData) => {
    options.onStreamError?.();
    messages.value.push({
      type: 'assistant',
      content: {
        content: errorData.error,
        timestamp: errorData.timestamp
      } as MessageContent,
    });
  };

  const handleTitleEvent = (titleData: TitleEventData) => {
    title.value = titleData.title;
  };

  const handlePlanEvent = (planData: PlanEventData) => {
    plan.value = planData;
  };

  const handleEvent = (event: AgentEvent): boolean => {
    // REST history replay and live WS catch-up can overlap. Logical event IDs
    // make the overlap harmless while transport cursors remain independently
    // usable for reconnecting.
    if (event.data.event_id && seenEventIds.has(event.data.event_id)) {
      lastEventId.value = event.data.transport_cursor ?? lastEventId.value;
      return false;
    }
    if (event.data.event_id) {
      seenEventIds.add(event.data.event_id);
    }

    // Control / live computer-panel events — not part of the chat message list
    if (
      event.event === 'status_update'
      || event.event === 'terminal_update'
      || event.event === 'file_update'
    ) {
      lastEventId.value = event.data.transport_cursor ?? event.data.event_id;
      return true;
    }
    if (event.event === 'message') {
      handleMessageEvent(event.data as MessageEventData);
    } else if (event.event === 'tool') {
      handleToolEvent(event.data as ToolEventData);
    } else if (event.event === 'step') {
      handleStepEvent(event.data as StepEventData);
    } else if (event.event === 'done') {
      // Loading state is cleared when the stream ends / status_update arrives
    } else if (event.event === 'wait') {
      // TODO: handle wait event
    } else if (event.event === 'error') {
      handleErrorEvent(event.data as ErrorEventData);
    } else if (event.event === 'title') {
      handleTitleEvent(event.data as TitleEventData);
    } else if (event.event === 'plan') {
      handlePlanEvent(event.data as PlanEventData);
    }
    lastEventId.value = event.data.transport_cursor ?? event.data.event_id;
    return true;
  };

  return { handleEvent, resetEventHistory };
}
