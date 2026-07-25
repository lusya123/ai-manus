// Backend API client configuration
import axios, { AxiosError } from 'axios';
import type { AxiosRequestConfig } from 'axios';
import { fetchEventSource } from '@microsoft/fetch-event-source';
import type { EventSourceMessage } from '@microsoft/fetch-event-source';
import {
  clearStoredTokens,
  getStoredExternalAuthToken,
  getStoredRefreshToken,
  getStoredToken,
  storeExternalAuthToken,
  storeRefreshToken,
  storeToken,
} from './auth';

// API configuration
export const API_CONFIG = {
  host: import.meta.env.VITE_API_URL || '',
  version: 'v1',
  timeout: 30000, // Request timeout in milliseconds
};

// Complete API base URL
export const BASE_URL = API_CONFIG.host 
  ? `${API_CONFIG.host}/api/${API_CONFIG.version}` 
  : `/api/${API_CONFIG.version}`;

// Login page route name/path
const LOGIN_ROUTE = '/login';

// Unified response format
export interface ApiResponse<T> {
  code: number;
  msg: string;
  data: T;
}

// Error format
export interface ApiError {
  code: number;
  message: string;
  details?: unknown;
}

export interface ApiClientRequestConfig extends AxiosRequestConfig {
  __skipAuth?: boolean;
  /** Do not turn a candidate-token verification failure into a refresh flow. */
  __skipAuthRefresh?: boolean;
  __suppressErrorLog?: boolean;
}

// Create axios instance
export const apiClient = axios.create({
  baseURL: BASE_URL,
  timeout: API_CONFIG.timeout,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Request interceptor, add authentication token
apiClient.interceptors.request.use(
  (config) => {
    const requestConfig = config as ApiClientRequestConfig;
    // Add authentication token if available
    const token = getStoredToken();
    if (requestConfig.__skipAuth) {
      config.headers.delete('Authorization');
    } else if (token && !config.headers.Authorization) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Track if we're currently refreshing token to prevent multiple concurrent requests
let isRefreshing = false;
let failedQueue: any[] = [];

const processQueue = (error: any, token: string | null = null) => {
  failedQueue.forEach(({ resolve, reject }) => {
    if (error) {
      reject(error);
    } else {
      resolve(token);
    }
  });
  
  failedQueue = [];
};

/**
 * Redirect to the login page with a full page load.
 * Intentionally avoids importing the router to keep the API layer
 * free of dependencies on the app entry point.
 */
const redirectToLogin = () => {
  if (window.location.pathname === LOGIN_ROUTE) {
    return; // Already on login page, no need to redirect
  }

  setTimeout(() => {
    window.location.href = LOGIN_ROUTE;
  }, 100);
};

/**
 * Common token refresh logic used by both axios interceptor and SSE connections
 */
export const refreshAuthToken = async (): Promise<string | null> => {
  if (isRefreshing) {
    // If already refreshing, queue this request
    return new Promise((resolve, reject) => {
      failedQueue.push({ resolve, reject });
    });
  }

  isRefreshing = true;
  const refreshToken = getStoredRefreshToken();
  
  if (!refreshToken) {
    // No refresh token available, clear auth and redirect to login
    clearStoredTokens();
    delete apiClient.defaults.headers.Authorization;
    window.dispatchEvent(new CustomEvent('auth:logout'));
    redirectToLogin();
    isRefreshing = false;
    throw new Error('No refresh token available');
  }

  try {
    // Attempt to refresh token
    const response = await apiClient.post('/auth/refresh', {
      refresh_token: refreshToken
    }, {
      // Add special marker to prevent interceptor from retrying this request
      __isRefreshRequest: true
    } as any);
    
    if (response.data && response.data.data) {
      const newAccessToken = response.data.data.access_token;
      storeToken(newAccessToken);
      if (response.data.data.refresh_token) {
        storeRefreshToken(response.data.data.refresh_token);
      }
      if (getStoredExternalAuthToken()) {
        storeExternalAuthToken(newAccessToken);
      }
      
      // Update default headers
      apiClient.defaults.headers.Authorization = `Bearer ${newAccessToken}`;
      
      // Process queued requests
      processQueue(null, newAccessToken);
      
      return newAccessToken;
    } else {
      throw new Error('Invalid refresh response');
    }
  } catch (refreshError) {
    // Refresh token failed, clear tokens and redirect to login
    clearStoredTokens();
    delete apiClient.defaults.headers.Authorization;
    
    processQueue(refreshError, null);
    
    // Emit logout event
    window.dispatchEvent(new CustomEvent('auth:logout'));
    
    // Redirect to login page
    redirectToLogin();
    
    throw refreshError;
  } finally {
    isRefreshing = false;
  }
};

// Response interceptor, unified error handling and token refresh
apiClient.interceptors.response.use(
  (response) => {
    // Check backend response format
    if (response.data && typeof response.data.code === 'number') {
      // If it's a business logic error (code not 0), convert to error handling
      if (response.data.code !== 0) {
        const apiError: ApiError = {
          code: response.data.code,
          message: response.data.msg || 'Unknown error',
          details: response.data
        };
        return Promise.reject(apiError);
      }
    }
    return response;
  },
  async (error: AxiosError) => {
    const originalRequest = error.config as any;
    
    // Skip retry logic for refresh requests to prevent infinite loops
    if (originalRequest.__isRefreshRequest) {
      const apiError: ApiError = {
        code: error.response?.status || 500,
        message: 'Token refresh failed',
        details: error.response?.data
      };
      console.error('Refresh token request failed:', apiError);
      return Promise.reject(apiError);
    }
    
    // Handle 401 Unauthorized errors with token refresh
    if (
      error.response?.status === 401
      && !originalRequest.__skipAuth
      && !originalRequest.__skipAuthRefresh
      && !originalRequest._retry
    ) {
      originalRequest._retry = true;

      try {
        const newAccessToken = await refreshAuthToken();
        if (newAccessToken) {
          // Retry original request with new token
          originalRequest.headers.Authorization = `Bearer ${newAccessToken}`;
          return apiClient(originalRequest);
        }
      } catch (refreshError) {
        // Token refresh failed, error already handled in refreshAuthToken
        console.error('Token refresh failed:', refreshError);
      }
    }

    const apiError: ApiError = {
      code: 500,
      message: 'Request failed',
    };

    if (error.response) {
      const status = error.response.status;
      apiError.code = status;
      
      // Try to extract detailed error information from response content
      if (error.response.data && typeof error.response.data === 'object') {
        const data = error.response.data as any;
        if (data.code && data.msg) {
          apiError.code = data.code;
          apiError.message = data.msg;
        } else {
          apiError.message = data.message || error.response.statusText || 'Request failed';
        }
        apiError.details = data;
      } else {
        apiError.message = error.response.statusText || 'Request failed';
      }
    } else if (error.request) {
      apiError.code = 503;
      apiError.message = 'Network error, please check your connection';
    }

    if (!originalRequest?.__suppressErrorLog) {
      console.error('API Error:', apiError);
    }
    return Promise.reject(apiError);
  }
); 

export interface SSECallbacks<T = any> {
  onOpen?: () => void;
  /**
   * Return false when this is a replay of an event the caller already applied.
   * Only newly applied business events reset the consecutive retry budget.
   */
  onMessage?: (event: { event: string; data: T }) => boolean | void;
  onClose?: () => void;
  /** Called once when the SSE connection has stopped permanently. */
  onError?: (error: Error) => void;
  /** Called before each bounded retry so callers can expose reconnect state. */
  onRetry?: (info: SSERetryInfo) => void;
}

export interface SSERetryInfo {
  attempt: number;
  elapsedMs: number;
  error: Error;
  nextRetryMs: number;
}

export interface SSEOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: any;
  headers?: Record<string, string>;
  /** Maximum transient retries during one connection lifecycle. */
  maxRetryAttempts?: number;
  /** Maximum time spent waiting for one retry episode to reopen successfully. */
  maxRetryDurationMs?: number;
  retryIntervalMs?: number;
  /**
   * When provided, a clean EOF before one of these events is treated as an
   * interrupted stream and retried. Receiving a terminal event closes the
   * transport immediately after delivering it to the caller.
   */
  terminalEvents?: readonly string[];
}

class FatalSSEError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'FatalSSEError';
  }
}

class RetrySSEWithFreshAuthError extends Error {
  constructor() {
    super('Retry SSE connection with refreshed credentials');
    this.name = 'RetrySSEWithFreshAuthError';
  }
}

const toError = (error: unknown): Error => (
  error instanceof Error ? error : new Error(String(error))
);

const isRetryableSSEStatus = (status: number): boolean => (
  status === 408
  || status === 425
  || status === 429
  || status >= 500
);

const DEFAULT_SSE_MAX_RETRY_ATTEMPTS = 5;
const DEFAULT_SSE_MAX_RETRY_DURATION_MS = 30_000;
const DEFAULT_SSE_RETRY_INTERVAL_MS = 1_000;

/**
 * Generic SSE connection function
 * @param endpoint - API endpoint (relative to BASE_URL)
 * @param options - Request options
 * @param callbacks - Event callbacks
 * @returns Function to cancel the SSE connection
 */
export const createSSEConnection = async <T = any>(
  endpoint: string,
  options: SSEOptions = {},
  callbacks: SSECallbacks<T> = {}
): Promise<() => void> => {
  const { onOpen, onMessage, onClose, onError, onRetry } = callbacks;
  const { 
    method = 'GET', 
    body, 
    headers = {},
    maxRetryAttempts = DEFAULT_SSE_MAX_RETRY_ATTEMPTS,
    maxRetryDurationMs = DEFAULT_SSE_MAX_RETRY_DURATION_MS,
    retryIntervalMs = DEFAULT_SSE_RETRY_INTERVAL_MS,
    terminalEvents = [],
  } = options;
  const retryLimit = Math.max(0, Math.floor(maxRetryAttempts));
  const retryDuration = Math.max(1, Math.floor(maxRetryDurationMs));
  const retryInterval = Math.max(0, Math.floor(retryIntervalMs));
  const terminalEventNames = new Set(terminalEvents);
  const requiresTerminalEvent = terminalEventNames.size > 0;
  
  // Create AbortController for cancellation
  const abortController = new AbortController();
  const apiUrl = `${BASE_URL}${endpoint}`;
  const hasExplicitAuthorization = Object.keys(headers).some(
    (header) => header.toLowerCase() === 'authorization'
  );

  // Add authentication headers
  const requestHeaders: Record<string, string> = {
    'Content-Type': 'application/json',
    ...headers,
  };

  // Add authentication token if available
  const token = getStoredToken();
  if (token && !hasExplicitAuthorization) {
    requestHeaders.Authorization = `Bearer ${token}`;
  }

  // fetch-event-source clones its headers once, so update Authorization at
  // request time. This lets its own retry loop use a token refreshed after a
  // 401 without starting a second, competing SSE lifecycle.
  const fetchWithCurrentAuth: typeof fetch = (input, init = {}) => {
    const latestHeaders = new Headers(init.headers);
    if (!hasExplicitAuthorization) {
      const latestToken = getStoredToken();
      if (latestToken) {
        latestHeaders.set('Authorization', `Bearer ${latestToken}`);
      } else {
        latestHeaders.delete('Authorization');
      }
    }
    return window.fetch(input, { ...init, headers: latestHeaders });
  };

  let authRefreshAttempted = false;
  let terminalCallbackSent = false;
  let retryAttempts = 0;
  let retryStartedAt: number | null = null;
  let retryDeadlineTimer: number | undefined;

  const clearRetryDeadline = () => {
    if (retryDeadlineTimer !== undefined) {
      window.clearTimeout(retryDeadlineTimer);
      retryDeadlineTimer = undefined;
    }
    retryStartedAt = null;
  };

  const finishWithError = (error: Error) => {
    if (terminalCallbackSent || abortController.signal.aborted) return;
    terminalCallbackSent = true;
    clearRetryDeadline();
    try {
      onError?.(error);
    } finally {
      abortController.abort();
    }
  };

  const finishNormally = () => {
    if (terminalCallbackSent || abortController.signal.aborted) return;
    terminalCallbackSent = true;
    clearRetryDeadline();
    try {
      onClose?.();
    } finally {
      abortController.abort();
    }
  };

  const startRetryDeadline = (cause: Error) => {
    if (retryStartedAt !== null) return;
    retryStartedAt = Date.now();
    retryDeadlineTimer = window.setTimeout(() => {
      finishWithError(new FatalSSEError(
        `SSE retry deadline exceeded after ${retryDuration}ms: ${cause.message}`,
      ));
    }, retryDuration);
  };

  const scheduleRetry = (error: Error, nextRetryMs: number): number => {
    retryAttempts += 1;
    if (retryAttempts > retryLimit) {
      const exhausted = new FatalSSEError(
        `SSE retry limit exceeded after ${retryLimit} attempts: ${error.message}`,
      );
      finishWithError(exhausted);
      throw exhausted;
    }

    startRetryDeadline(error);
    const elapsedMs = retryStartedAt === null ? 0 : Date.now() - retryStartedAt;
    try {
      onRetry?.({
        attempt: retryAttempts,
        elapsedMs,
        error,
        nextRetryMs,
      });
    } catch {
      const callbackError = new FatalSSEError('SSE retry callback failed');
      finishWithError(callbackError);
      throw callbackError;
    }
    return nextRetryMs;
  };

  const connectionPromise = fetchEventSource(apiUrl, {
    method,
    headers: requestHeaders,
    openWhenHidden: true,
    body: body ? JSON.stringify(body) : undefined,
    signal: abortController.signal,
    fetch: fetchWithCurrentAuth,
    async onopen(response) {
      if (terminalCallbackSent || abortController.signal.aborted) return;
      if (response.status === 401) {
        if (authRefreshAttempted) {
          throw new FatalSSEError('SSE authentication failed after token refresh');
        }
        authRefreshAttempted = true;
        try {
          const newAccessToken = await refreshAuthToken();
          if (!newAccessToken) {
            throw new Error('Token refresh returned no access token');
          }
          window.dispatchEvent(new CustomEvent('auth:token-refreshed'));
        } catch {
          throw new FatalSSEError('SSE token refresh failed');
        }
        throw new RetrySSEWithFreshAuthError();
      }

      if (!response.ok) {
        const message = `SSE request failed with HTTP ${response.status}`;
        if (isRetryableSSEStatus(response.status)) {
          throw new Error(message);
        }
        throw new FatalSSEError(message);
      }

      const contentType = response.headers.get('content-type');
      if (!contentType?.startsWith('text/event-stream')) {
        throw new FatalSSEError('SSE response has an invalid content type');
      }

      authRefreshAttempted = false;
      // Response headers prove that this retry episode successfully reopened.
      // Keep the cumulative attempt count for terminal streams so repeated
      // open-then-EOF cycles still reach a finite limit.
      clearRetryDeadline();
      if (!requiresTerminalEvent) {
        retryAttempts = 0;
      }
      try {
        onOpen?.();
      } catch {
        throw new FatalSSEError('SSE open callback failed');
      }
    },
    onmessage(event: EventSourceMessage) {
      if (terminalCallbackSent || abortController.signal.aborted) return;
      if (!event.event || event.event.trim() === '') return;

      let data: T;
      try {
        data = JSON.parse(event.data) as T;
      } catch {
        throw new FatalSSEError('SSE event contains invalid JSON');
      }

      let madeProgress: boolean;
      try {
        madeProgress = onMessage?.({ event: event.event, data }) !== false;
      } catch {
        throw new FatalSSEError('SSE message callback failed');
      }

      if (madeProgress) {
        retryAttempts = 0;
        clearRetryDeadline();
      }

      if (terminalEventNames.has(event.event)) {
        finishNormally();
      }
    },
    onclose() {
      if (terminalCallbackSent || abortController.signal.aborted) return;
      if (requiresTerminalEvent) {
        throw new Error('SSE connection closed before a terminal event');
      }
      finishNormally();
    },
    onerror(error: unknown) {
      const normalizedError = toError(error);
      if (normalizedError instanceof RetrySSEWithFreshAuthError) {
        return scheduleRetry(normalizedError, 0);
      }
      if (normalizedError instanceof FatalSSEError || terminalCallbackSent) {
        finishWithError(normalizedError);
        throw normalizedError;
      }

      return scheduleRetry(normalizedError, retryInterval);
    },
  });

  connectionPromise.catch((error: unknown) => {
    if (abortController.signal.aborted) return;
    const normalizedError = toError(error);
    finishWithError(normalizedError);
    console.error('SSE connection failed:', normalizedError);
  });

  return () => {
    if (abortController.signal.aborted) return;
    terminalCallbackSent = true;
    clearRetryDeadline();
    abortController.abort();
  };
};
