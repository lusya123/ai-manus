import {
  apiClient,
  ApiResponse,
  BASE_URL,
  refreshAuthToken,
} from './client';
import type { ApiClientRequestConfig } from './client';
import { getStoredToken } from './auth';

export type ClawStatus = 'creating' | 'running' | 'stopped' | 'error';

export interface Claw {
  id: string;
  user_id: string;
  status: ClawStatus;
  container_name?: string;
  error_message?: string;
  expires_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ClawEvent {
  type: 'auth_ack' | 'text' | 'thinking' | 'done' | 'error' | 'file' | 'catchup' | 'heartbeat' | 'status';
  content?: string;
  stop_reason?: string;
  error?: string;
  status?: ClawStatus;
  file_id?: string;
  filename?: string;
  content_type?: string;
  size?: number;
  upload_date?: string;
  file_url?: string;
}

export interface ClawChatAttachment {
  file_id: string;
  filename: string;
  content_type?: string;
  size: number;
  file_url?: string;
}

export interface ClawChatMessage {
  role: 'user' | 'assistant' | 'attachments';
  content: string;
  timestamp: number;
  attachments?: ClawChatAttachment[];
}

// ---- REST endpoints ----

export async function getClaw(): Promise<Claw> {
  const silentRequestConfig: ApiClientRequestConfig = { __suppressErrorLog: true };
  const response = await apiClient.get<ApiResponse<Claw>>(
    '/claw',
    silentRequestConfig,
  );
  return response.data.data;
}

export async function createClaw(): Promise<Claw> {
  const response = await apiClient.post<ApiResponse<Claw>>('/claw');
  return response.data.data;
}

export async function deleteClaw(): Promise<void> {
  await apiClient.delete<ApiResponse<Record<string, never>>>('/claw');
}

export async function getClawHistory(): Promise<ClawChatMessage[]> {
  const response = await apiClient.get<ApiResponse<{ messages: ClawChatMessage[] }>>('/claw/history');
  return response.data.data.messages;
}

// ---- WebSocket connection ----

export interface ClawWSCallbacks {
  onEvent: (event: ClawEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
}

const WS_AUTH_INVALID_CLOSE_CODE = 4001;
const WS_AUTH_EXPIRED_CLOSE_CODE = 4002;
type ClawTokenRefresher = () => Promise<string | null>;

/**
 * Manages a persistent WebSocket connection to the Claw backend.
 * Auto-reconnects on disconnect with exponential backoff.
 */
export class ClawWebSocket {
  private ws: WebSocket | null = null;
  private callbacks: ClawWSCallbacks;
  private closed = false;
  private authenticated = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectDelay = 1000;
  private refreshInFlight: Promise<string | null> | null = null;
  private readonly tokenRefresher: ClawTokenRefresher;

  constructor(
    callbacks: ClawWSCallbacks,
    tokenRefresher: ClawTokenRefresher = refreshAuthToken,
  ) {
    this.callbacks = callbacks;
    this.tokenRefresher = tokenRefresher;
    this.connect();
  }

  private connect() {
    if (this.closed) return;

    this.authenticated = false;

    const wsBase = BASE_URL.replace(/^http/, 'ws');
    const url = `${wsBase}/claw/ws`;

    const socket = new WebSocket(url);
    this.ws = socket;

    socket.onopen = () => {
      if (this.closed || this.ws !== socket) return;
      // Authenticate in the first WebSocket frame. Putting the primary bearer
      // token in the URL leaks it to browser history and access logs.
      socket.send(JSON.stringify({ type: 'auth', token: getStoredToken() }));
    };

    socket.onmessage = (e) => {
      if (this.closed || this.ws !== socket) return;
      try {
        const data: ClawEvent = JSON.parse(e.data);
        if (data.type === 'auth_ack') {
          if (!this.authenticated) {
            this.authenticated = true;
            this.reconnectDelay = 1000;
            this.callbacks.onOpen?.();
          }
          return;
        }
        if (!this.authenticated) return;
        if (data.type !== 'heartbeat') {
          this.callbacks.onEvent(data);
        }
      } catch {
        // ignore
      }
    };

    socket.onclose = (event: CloseEvent) => {
      if (this.ws !== socket) return;
      this.ws = null;
      this.authenticated = false;
      this.callbacks.onClose?.();

      if (this.closed) return;
      if (event.code === WS_AUTH_INVALID_CLOSE_CODE) {
        // Revoked, disabled, or otherwise invalid credentials are terminal
        // for this connection. Retrying the same token creates a permanent
        // unauthorized reconnect loop and must never trigger token refresh.
        this.closed = true;
        if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
        return;
      }
      if (event.code === WS_AUTH_EXPIRED_CLOSE_CODE) {
        void this.refreshExpiredTokenAndReconnect();
        return;
      }
      this.scheduleReconnect();
    };

    socket.onerror = () => {
      if (this.ws === socket) socket.close();
    };
  }

  private async refreshExpiredTokenAndReconnect() {
    if (this.closed) return;
    if (!this.refreshInFlight) {
      this.refreshInFlight = this.tokenRefresher().finally(() => {
        this.refreshInFlight = null;
      });
    }
    const refresh = this.refreshInFlight;

    try {
      const token = await refresh;
      if (!token) throw new Error('Token refresh returned no access token');
      if (this.closed || this.ws) return;
      this.reconnectDelay = 1000;
      this.connect();
    } catch {
      // refreshAuthToken performs the shared logout/cleanup flow. Mark this
      // socket terminal as well so no network close can schedule a retry.
      this.closed = true;
    }
  }

  private scheduleReconnect() {
    if (this.closed || this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30000);
      this.connect();
    }, this.reconnectDelay);
  }

  /**
   * Send a chat message through the WebSocket, optionally with file attachments.
   */
  send(message: string, sessionId: string = 'default', fileIds?: string[]) {
    if (this.authenticated && this.ws?.readyState === WebSocket.OPEN) {
      const payload: Record<string, unknown> = { type: 'chat', message, session_id: sessionId };
      if (fileIds && fileIds.length > 0) {
        payload.file_ids = fileIds;
      }
      this.ws.send(JSON.stringify(payload));
    }
  }

  /**
   * Close the connection permanently (no auto-reconnect).
   */
  disconnect() {
    this.closed = true;
    this.authenticated = false;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    const socket = this.ws;
    this.ws = null;
    socket?.close();
  }

  get isConnected() {
    return this.authenticated && this.ws?.readyState === WebSocket.OPEN;
  }
}
