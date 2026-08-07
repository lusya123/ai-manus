// Authentication API service
import { apiClient, ApiResponse } from './client';
import type { ApiClientRequestConfig } from './client';
import {
  captureAgentConfigFromUrl,
  commitAgentConfigHandoff,
} from './agentConfig';
import type { CapturedAgentConfigHandoff } from './agentConfig';

/**
 * User role type
 */
export type UserRole = 'admin' | 'user';

/**
 * User response type
 */
export interface User {
  id: string;
  fullname: string;
  email: string;
  role: UserRole;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  last_login_at?: string;
  auth_provider?: string | null;
  external_id?: string | null;
  external_user?: Record<string, unknown> | null;
}

/**
 * Login request type
 */
export interface LoginRequest {
  email: string;
  password: string;
}

/**
 * Register request type
 */
export interface RegisterRequest {
  fullname: string;
  email: string;
  password: string;
}

/**
 * Login response type
 */
export interface LoginResponse {
  user: User;
  access_token: string;
  refresh_token: string;
  token_type: string;
}

/**
 * Register response type
 */
export interface RegisterResponse {
  user: User;
  access_token: string;
  refresh_token: string;
  token_type: string;
}

/**
 * Change password request type
 */
export interface ChangePasswordRequest {
  old_password: string;
  new_password: string;
}

/**
 * Change fullname request type
 */
export interface ChangeFullnameRequest {
  fullname: string;
}

/**
 * Refresh token request type
 */
export interface RefreshTokenRequest {
  refresh_token: string;
}

export interface LogoutRequest {
  refresh_token?: string;
}

/**
 * Refresh token response type
 */
export interface RefreshTokenResponse {
  access_token: string;
  refresh_token?: string;
  token_type: string;
}

/**
 * Auth status response type
 */
export interface AuthStatusResponse {
  authenticated: boolean;
  user?: User;
  auth_provider: string;
}

/**
 * Resource access token request type
 */
export interface AccessTokenRequest {
  resource_type: 'file' | 'vnc';
  resource_id: string;
  expire_minutes?: number;
}

/**
 * Resource access token response type
 */
export interface AccessTokenResponse {
  access_token: string;
  resource_type: string;
  resource_id: string;
  expires_in: number;
}

/**
 * Send verification code request type
 */
export interface SendVerificationCodeRequest {
  email: string;
}

/**
 * Reset password request type
 */
export interface ResetPasswordRequest {
  email: string;
  verification_code: string;
  new_password: string;
}



/**
 * User login
 * @param request Login credentials
 * @returns Login response with user info and tokens
 */
export async function login(request: LoginRequest): Promise<LoginResponse> {
  const response = await apiClient.post<ApiResponse<LoginResponse>>('/auth/login', request);
  return response.data.data;
}

/**
 * User registration
 * @param request Registration data
 * @returns Registration response with user info and tokens
 */
export async function register(request: RegisterRequest): Promise<RegisterResponse> {
  const response = await apiClient.post<ApiResponse<RegisterResponse>>('/auth/register', request);
  return response.data.data;
}

/**
 * Get authentication status
 * @returns Current authentication status and configuration
 */
export async function getAuthStatus(): Promise<AuthStatusResponse> {
  const response = await apiClient.get<ApiResponse<AuthStatusResponse>>('/auth/status');
  return response.data.data;
}

/**
 * Change user password
 * @param request Change password data
 * @returns Success response
 */
export async function changePassword(request: ChangePasswordRequest): Promise<Record<string, never>> {
  const response = await apiClient.post<ApiResponse<Record<string, never>>>('/auth/change-password', request);
  return response.data.data;
}

/**
 * Change user fullname
 * @param request Change fullname data
 * @returns Updated user data
 */
export async function changeFullname(request: ChangeFullnameRequest): Promise<User> {
  const response = await apiClient.post<ApiResponse<User>>('/auth/change-fullname', request);
  return response.data.data;
}

/**
 * Get current user information
 * @returns Current user data
 */
export async function getCurrentUser(): Promise<User> {
  const response = await apiClient.get<ApiResponse<User>>('/auth/me');
  return response.data.data;
}

/**
 * Get user by ID (admin only)
 * @param userId User ID to fetch
 * @returns User data
 */
export async function getUser(userId: string): Promise<User> {
  const response = await apiClient.get<ApiResponse<User>>(`/auth/user/${userId}`);
  return response.data.data;
}

/**
 * Deactivate user account (admin only)
 * @param userId User ID to deactivate
 * @returns Success response
 */
export async function deactivateUser(userId: string): Promise<Record<string, never>> {
  const response = await apiClient.post<ApiResponse<Record<string, never>>>(`/auth/user/${userId}/deactivate`);
  return response.data.data;
}

/**
 * Activate user account (admin only)
 * @param userId User ID to activate
 * @returns Success response
 */
export async function activateUser(userId: string): Promise<Record<string, never>> {
  const response = await apiClient.post<ApiResponse<Record<string, never>>>(`/auth/user/${userId}/activate`);
  return response.data.data;
}

/**
 * Refresh access token
 * @param request Refresh token data
 * @returns New access token
 */
export async function refreshToken(request: RefreshTokenRequest): Promise<RefreshTokenResponse> {
  const response = await apiClient.post<ApiResponse<RefreshTokenResponse>>('/auth/refresh', request);
  return response.data.data;
}

/**
 * User logout
 * @returns Success response
 */
export async function logout(request: LogoutRequest = {}): Promise<Record<string, never>> {
  const response = await apiClient.post<ApiResponse<Record<string, never>>>(
    '/auth/logout',
    request,
    { __skipAuthRefresh: true } as ApiClientRequestConfig,
  );
  return response.data.data;
}

/**
 * Send verification code for password reset
 * @param request Email to send verification code to
 * @returns Success response
 */
export async function sendVerificationCode(request: SendVerificationCodeRequest): Promise<Record<string, never>> {
  const response = await apiClient.post<ApiResponse<Record<string, never>>>('/auth/send-verification-code', request);
  return response.data.data;
}

/**
 * Reset password with verification code
 * @param request Reset password data including email, verification code and new password
 * @returns Success response
 */
export async function resetPassword(request: ResetPasswordRequest): Promise<Record<string, never>> {
  const response = await apiClient.post<ApiResponse<Record<string, never>>>('/auth/reset-password', request);
  return response.data.data;
}



/**
 * Set authentication token in request headers
 * @param token JWT access token
 */
export function setAuthToken(token: string): void {
  apiClient.defaults.headers.Authorization = `Bearer ${token}`;
}

/**
 * Clear authentication token from request headers
 */
export function clearAuthToken(): void {
  delete apiClient.defaults.headers.Authorization;
}

/**
 * Get stored authentication token from localStorage
 * @returns Stored token or null
 */
export function getStoredToken(): string | null {
  return localStorage.getItem('access_token');
}

const EXTERNAL_AUTH_SUPPRESSION_KEY = 'sub2api_external_auth_suppressed';
const EXTERNAL_AUTH_STATE_KEY = 'sub2api_external_auth_state';

export function isExternalAuthHydrationSuppressed(): boolean {
  return sessionStorage.getItem(EXTERNAL_AUTH_SUPPRESSION_KEY) === '1';
}

/**
 * Return the token shared by a same-origin Sub2API host application.
 */
export function getStoredExternalAuthToken(): string | null {
  if (isExternalAuthHydrationSuppressed()) {
    return null;
  }
  return localStorage.getItem('sub2api_auth_token') || localStorage.getItem('auth_token');
}

/**
 * Persist the last Sub2API access token independently from local-auth tokens.
 */
export function storeExternalAuthToken(token: string): void {
  sessionStorage.removeItem(EXTERNAL_AUTH_SUPPRESSION_KEY);
  localStorage.setItem('sub2api_auth_token', token);
}

/**
 * Store authentication token in localStorage
 * @param token Token to store
 */
export function storeToken(token: string): void {
  localStorage.setItem('access_token', token);
}

/**
 * Store refresh token in localStorage
 * @param refreshToken Refresh token to store
 */
export function storeRefreshToken(refreshToken: string): void {
  localStorage.setItem('refresh_token', refreshToken);
}

/**
 * Get stored refresh token from localStorage
 * @returns Stored refresh token or null
 */
export function getStoredRefreshToken(): string | null {
  return localStorage.getItem('refresh_token');
}

/**
 * Clear stored tokens from localStorage
 */
export function clearStoredTokens(): void {
  localStorage.removeItem('access_token');
  localStorage.removeItem('refresh_token');
  localStorage.removeItem('sub2api_auth_token');
  // Do not immediately re-import a same-origin host token after an explicit
  // logout or an authentication failure in this browser tab.
  sessionStorage.setItem(EXTERNAL_AUTH_SUPPRESSION_KEY, '1');
}

const EXTERNAL_TOKEN_PARAMS = [
  'manus_access_token',
  'access_token',
  'auth_token',
  'token',
] as const;

const EXTERNAL_AUTH_PARAMS = [
  ...EXTERNAL_TOKEN_PARAMS,
  'refresh_token',
  'token_type',
  'expires_in',
] as const;

const EXTERNAL_AUTH_STATE_PARAM = 'state';

export interface PendingExternalAuthHandoff {
  accessToken: string;
  refreshToken: string | null;
  agentConfig: CapturedAgentConfigHandoff;
}

const capturedExternalHandoffs = new WeakSet<PendingExternalAuthHandoff>();

function readTokenParam(params: URLSearchParams): string | null {
  for (const key of EXTERNAL_TOKEN_PARAMS) {
    const token = params.get(key);
    if (token) return token;
  }
  return null;
}

export function hasExternalAuthTokenInFragment(): boolean {
  const rawHash = window.location.hash.startsWith('#')
    ? window.location.hash.slice(1)
    : window.location.hash;
  return Boolean(readTokenParam(new URLSearchParams(rawHash)));
}

function generateExternalAuthState(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi?.getRandomValues) {
    const bytes = new Uint8Array(32);
    cryptoApi.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  }

  // Older WebViews may expose randomUUID without getRandomValues. Two UUIDs
  // retain ample entropy for a login correlation nonce.
  const randomUUID = (cryptoApi as Crypto & { randomUUID?: () => string } | undefined)?.randomUUID;
  if (randomUUID) {
    return `${randomUUID.call(cryptoApi)}${randomUUID.call(cryptoApi)}`.replace(/-/g, '');
  }

  // Never downgrade a CSRF nonce to Math.random(). Refusing to start the
  // handoff is safer than creating a predictable account-switch link.
  throw new Error('Secure random number generation is unavailable');
}

/**
 * Create a Sub2API login URL and remember a one-time correlation state for the
 * callback in this browser tab.
 */
export function buildSub2ApiLoginUrl(loginUrl: string, redirectUri: string): string {
  const state = generateExternalAuthState();
  sessionStorage.setItem(EXTERNAL_AUTH_STATE_KEY, state);
  sessionStorage.removeItem(EXTERNAL_AUTH_SUPPRESSION_KEY);

  const url = new URL(loginUrl, window.location.origin);
  // Put the state in redirect_uri as well as the standard top-level parameter.
  // This preserves correlation with simple handoff hosts that append a token
  // fragment to redirect_uri but do not implement OAuth state reflection.
  const callbackUrl = new URL(redirectUri, window.location.origin);
  callbackUrl.searchParams.set(EXTERNAL_AUTH_STATE_PARAM, state);
  url.searchParams.set('redirect_uri', callbackUrl.toString());
  url.searchParams.set(EXTERNAL_AUTH_STATE_PARAM, state);
  return url.toString();
}

function removeExternalAuthParams(params: URLSearchParams): boolean {
  let changed = false;
  for (const key of EXTERNAL_AUTH_PARAMS) {
    if (params.has(key)) {
      params.delete(key);
      changed = true;
    }
  }
  return changed;
}

function cleanExternalAuthUrl(
  searchParams: URLSearchParams,
  hashParams: URLSearchParams,
  removeState: boolean,
): void {
  const searchChanged = removeExternalAuthParams(searchParams);
  const hashChanged = removeExternalAuthParams(hashParams);
  let stateChanged = false;
  if (removeState) {
    if (searchParams.has(EXTERNAL_AUTH_STATE_PARAM)) {
      searchParams.delete(EXTERNAL_AUTH_STATE_PARAM);
      stateChanged = true;
    }
    if (hashParams.has(EXTERNAL_AUTH_STATE_PARAM)) {
      hashParams.delete(EXTERNAL_AUTH_STATE_PARAM);
      stateChanged = true;
    }
  }

  if (searchChanged || hashChanged || stateChanged) {
    const search = searchParams.toString();
    const hash = hashParams.toString();
    const cleanUrl = `${window.location.pathname}${search ? `?${search}` : ''}${hash ? `#${hash}` : ''}`;
    window.history.replaceState(window.history.state, document.title, cleanUrl);
  }
}

/**
 * Capture a fragment handoff and immediately remove credentials from the URL.
 * A matching state is consumed before token verification, so even a failed
 * verification cannot be replayed.
 */
export function captureExternalAuthHandoff(): PendingExternalAuthHandoff | null {
  // Capture model fields first. This removes them from the URL without writing
  // model state; they are committed only after /auth/me accepts the token.
  const agentConfig = captureAgentConfigFromUrl();
  const searchParams = new URLSearchParams(window.location.search);
  const rawHash = window.location.hash.startsWith('#')
    ? window.location.hash.slice(1)
    : window.location.hash;
  const hashParams = new URLSearchParams(rawHash);
  const fragmentToken = readTokenParam(hashParams);
  const refreshToken = fragmentToken ? hashParams.get('refresh_token') : null;
  const hasAuthParams = EXTERNAL_AUTH_PARAMS.some(
    (key) => searchParams.has(key) || hashParams.has(key),
  );
  const callbackState = hashParams.get(EXTERNAL_AUTH_STATE_PARAM)
    || searchParams.get(EXTERNAL_AUTH_STATE_PARAM);

  cleanExternalAuthUrl(searchParams, hashParams, hasAuthParams);
  if (!hasAuthParams) return null;

  const expectedState = sessionStorage.getItem(EXTERNAL_AUTH_STATE_KEY);
  if (!expectedState || !callbackState || callbackState !== expectedState) {
    return null;
  }

  // Consume the state on every correctly correlated callback, including an
  // invalid/malformed token response.
  sessionStorage.removeItem(EXTERNAL_AUTH_STATE_KEY);

  // Account replacement is never implicit. A callback is accepted only when
  // Manus initiated a sign-in while this tab had no authenticated account.
  if (getStoredToken() || !fragmentToken) {
    return null;
  }

  const handoff = { accessToken: fragmentToken, refreshToken, agentConfig };
  capturedExternalHandoffs.add(handoff);
  return handoff;
}

export async function verifyExternalAuthToken(token: string): Promise<User> {
  const config: ApiClientRequestConfig = {
    headers: { Authorization: `Bearer ${token}` },
    __skipAuthRefresh: true,
    __suppressErrorLog: true,
  };
  const response = await apiClient.get<ApiResponse<User>>('/auth/me', config);
  const user = response.data?.data;
  if (!user || typeof user.id !== 'string' || !user.id) {
    throw new Error('Invalid current-user response');
  }
  return user;
}

function restoreStorageValue(storage: Storage, key: string, value: string | null): void {
  if (value === null) storage.removeItem(key);
  else storage.setItem(key, value);
}

function commitExternalAuthCredentials(
  handoff: Pick<PendingExternalAuthHandoff, 'accessToken' | 'refreshToken'>,
  commitModel: () => void,
): void {
  const previousAccessToken = localStorage.getItem('access_token');
  const previousRefreshToken = localStorage.getItem('refresh_token');
  const previousExternalToken = localStorage.getItem('sub2api_auth_token');
  const previousSuppression = sessionStorage.getItem(EXTERNAL_AUTH_SUPPRESSION_KEY);
  const previousAuthorization = apiClient.defaults.headers.Authorization;

  try {
    localStorage.setItem('access_token', handoff.accessToken);
    localStorage.setItem('sub2api_auth_token', handoff.accessToken);
    if (handoff.refreshToken) localStorage.setItem('refresh_token', handoff.refreshToken);
    else localStorage.removeItem('refresh_token');
    sessionStorage.removeItem(EXTERNAL_AUTH_SUPPRESSION_KEY);
    setAuthToken(handoff.accessToken);
    commitModel();
  } catch (error) {
    restoreStorageValue(localStorage, 'access_token', previousAccessToken);
    restoreStorageValue(localStorage, 'refresh_token', previousRefreshToken);
    restoreStorageValue(localStorage, 'sub2api_auth_token', previousExternalToken);
    restoreStorageValue(sessionStorage, EXTERNAL_AUTH_SUPPRESSION_KEY, previousSuppression);
    if (previousAuthorization) apiClient.defaults.headers.Authorization = previousAuthorization;
    else delete apiClient.defaults.headers.Authorization;
    throw error;
  }
}

/** Verify a captured callback, then commit credentials and model state together. */
export async function completeExternalAuthHandoff(
  handoff: PendingExternalAuthHandoff,
): Promise<boolean> {
  // The in-memory candidate is itself single-use. This also prevents callers
  // from bypassing state validation by constructing a lookalike object.
  if (!capturedExternalHandoffs.delete(handoff)) return false;
  try {
    await verifyExternalAuthToken(handoff.accessToken);
    commitExternalAuthCredentials(handoff, () => {
      commitAgentConfigHandoff(handoff.agentConfig, true);
    });
    return true;
  } catch {
    return false;
  }
}

/**
 * Import a same-origin Sub2API host token when no fragment callback is present.
 * It is validated just like a callback token, but does not replace model state.
 */
export async function hydrateStoredExternalAuthToken(): Promise<boolean> {
  if (getStoredToken()) return true;
  const token = getStoredExternalAuthToken();
  if (!token) return false;

  try {
    await verifyExternalAuthToken(token);
    commitExternalAuthCredentials(
      { accessToken: token, refreshToken: null },
      () => undefined,
    );
    return true;
  } catch {
    return false;
  }
}

/**
 * Import Sub2API authentication from a correlated callback or same-origin
 * storage. Kept as a convenience entry point for non-router callers.
 */
export async function hydrateExternalAuthToken(): Promise<boolean> {
  const handoff = captureExternalAuthHandoff();
  if (handoff) return completeExternalAuthHandoff(handoff);
  return hydrateStoredExternalAuthToken();
}

/**
 * Initialize authentication from stored tokens
 * This should be called when the app starts
 */
export function initializeAuth(): void {
  const token = getStoredToken();
  if (token) {
    setAuthToken(token);
  }
}
