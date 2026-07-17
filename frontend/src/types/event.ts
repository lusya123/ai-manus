import type { FileInfo } from '../api/file';

export type AgentSSEEvent = {
  event: 'accepted' | 'tool' | 'step' | 'message' | 'error' | 'done' | 'title' | 'wait' | 'plan' | 'attachments';
  data: AcceptedEventData | ToolEventData | StepEventData | MessageEventData | ErrorEventData | DoneEventData | TitleEventData | WaitEventData | PlanEventData;
}

export interface BaseEventData {
  event_id: string;
  turn_id?: string;
  transport_cursor?: string;
  timestamp: number;
}

export interface AcceptedEventData extends BaseEventData {
  submission_id: string;
  state: string;
}

export interface ToolEventData extends BaseEventData {
  tool_call_id: string;
  name: string;
  status: "calling" | "called";
  function: string;
  args: {[key: string]: any};
  content?: any;
}

export interface StepEventData extends BaseEventData {
  status: "pending" | "running" | "completed" | "failed"
  id: string
  description: string
}

export interface MessageEventData extends BaseEventData {
  content: string;
  role: "user" | "assistant";
  attachments: FileInfo[];
}

export interface ErrorEventData extends BaseEventData {
  error: string;
}

export type DoneEventData = BaseEventData

export type WaitEventData = BaseEventData

export interface TitleEventData extends BaseEventData {
  title: string;
}

export interface PlanEventData extends BaseEventData {
  steps: StepEventData[];
}
