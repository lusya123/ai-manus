<template>
  <SimpleBar ref="simpleBarRef" @scroll="handleScroll">
    <div ref="chatContainerRef" class="relative flex flex-col h-full flex-1 min-w-0 px-5">
      <div ref="observerRef"
        class="sm:min-w-[390px] flex flex-row items-center justify-between pt-3 pb-1 gap-1 sticky top-0 z-10 bg-[var(--background-gray-main)] flex-shrink-0">
        <div class="flex items-center flex-1"></div>
        <div class="max-w-full sm:max-w-[768px] sm:min-w-[390px] flex w-full flex-col gap-[4px] overflow-hidden">
          <div
            class="text-[var(--text-primary)] text-lg font-medium w-full flex flex-row items-center justify-between flex-1 min-w-0 gap-2">
            <div class="flex flex-row items-center gap-[6px] flex-1 min-w-0">
              <span class="whitespace-nowrap text-ellipsis overflow-hidden">
                {{ title }}
              </span>
            </div>
            <div class="flex items-center gap-2 flex-shrink-0">
              <span class="relative flex-shrink-0" aria-expanded="false" aria-haspopup="dialog">
                <Popover>
                  <PopoverTrigger>
                    <button
                      class="h-8 px-3 rounded-[100px] inline-flex items-center gap-1 clickable outline outline-1 outline-offset-[-1px] outline-[var(--border-btn-main)] hover:bg-[var(--fill-tsp-white-light)] me-1.5">
                      <ShareIcon color="var(--icon-secondary)" />
                      <span class="text-[var(--text-secondary)] text-sm font-medium">{{ t('Share') }}</span>
                    </button>
                  </PopoverTrigger>
                  <PopoverContent>
                    <div
                      class="w-[400px] flex flex-col rounded-2xl bg-[var(--background-menu-white)] shadow-[0px_8px_32px_0px_var(--shadow-S),0px_0px_0px_1px_var(--border-light)]"
                      style="max-width: calc(-16px + 100vw);">
                      <div class="flex flex-col pt-[12px] px-[16px] pb-[16px]">
                        <!-- Private mode option -->
                        <div @click="handleShareModeChange('private')"
                          :class="{'pointer-events-none opacity-50': sharingLoading}"
                          class="flex items-center gap-[10px] px-[8px] -mx-[8px] py-[8px] rounded-[8px] clickable hover:bg-[var(--fill-tsp-white-main)]">
                          <div
                            :class="shareMode === 'private' ? 'bg-[var(--Button-primary-black)]' : 'bg-[var(--fill-tsp-white-dark)]'"
                            class="w-[32px] h-[32px] rounded-[8px] flex items-center justify-center">
                            <Lock :size="16" :stroke="shareMode === 'private' ? 'var(--text-onblack)' : 'var(--icon-primary)'" :stroke-width="2" /></div>
                          <div class="flex flex-col flex-1 min-w-0">
                            <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Private Only') }}</div>
                            <div class="text-[13px] text-[var(--text-tertiary)]">{{ t('Only visible to you') }}</div>
                          </div><Check :size="20" :class="shareMode === 'private' ? 'ml-auto' : 'ml-auto invisible'" :color="shareMode === 'private' ? 'var(--icon-primary)' : 'var(--icon-tertiary)'" />
                        </div>
                        <!-- Public mode option -->
                        <div @click="handleShareModeChange('public')"
                          :class="{'pointer-events-none opacity-50': sharingLoading}"
                          class="flex items-center gap-[10px] px-[8px] -mx-[8px] py-[8px] rounded-[8px] clickable hover:bg-[var(--fill-tsp-white-main)]">
                          <div
                            :class="shareMode === 'public' ? 'bg-[var(--Button-primary-black)]' : 'bg-[var(--fill-tsp-white-dark)]'"
                            class="w-[32px] h-[32px] rounded-[8px] flex items-center justify-center">
                            <Globe :size="16" :stroke="shareMode === 'public' ? 'var(--text-onblack)' : 'var(--icon-primary)'" :stroke-width="2" /></div>
                          <div class="flex flex-col flex-1 min-w-0">
                            <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Public Access') }}</div>
                            <div class="text-[13px] text-[var(--text-tertiary)]">{{ t('Anyone with the link can view') }}</div>
                          </div><Check :size="20" :class="shareMode === 'public' ? 'ml-auto' : 'ml-auto invisible'" :color="shareMode === 'public' ? 'var(--icon-primary)' : 'var(--icon-tertiary)'" />
                        </div>
                        <div class="border-t border-[var(--border-main)] mt-[4px]"></div>
                        
                        <!-- Show instant share button when in private mode -->
                        <div v-if="shareMode === 'private'">
                          <button @click.stop="handleInstantShare"
                            :disabled="sharingLoading"
                            class="inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 bg-[var(--Button-primary-black)] text-[var(--text-onblack)] h-[36px] px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 mt-[16px] w-full disabled:opacity-50 disabled:cursor-not-allowed"
                            data-tabindex="" tabindex="-1">
                            <div v-if="sharingLoading" class="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
                            <Link v-else :size="16" stroke="currentColor" :stroke-width="2" />
                            {{ sharingLoading ? t('Sharing...') : t('Share Instantly') }}
                          </button>
                        </div>
                        
                        <!-- Show copy link button when in public mode -->
                        <div v-else>
                          <button @click.stop="handleCopyLink"
                            :class="linkCopied ? 'inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors active:opacity-80 bg-[var(--Button-primary-white)] text-[var(--text-primary)] hover:opacity-70 active:hover-60 h-[36px] px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 mt-[16px] w-full border border-[var(--border-btn-main)] shadow-none' : 'inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 bg-[var(--Button-primary-black)] text-[var(--text-onblack)] h-[36px] px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 mt-[16px] w-full'"
                            data-tabindex="" tabindex="-1">
                            <Link v-if="!linkCopied" :size="16" stroke="currentColor" :stroke-width="2" />
                            <Check v-else :size="16" color="var(--text-primary)" />
                            {{ linkCopied ? t('Link Copied') : t('Copy Link') }}
                          </button>
                        </div>
                      </div>
                    </div>
                  </PopoverContent>
                </Popover>
              </span>
              <button @click="handleFileListShow"
                class="p-[5px] flex items-center justify-center hover:bg-[var(--fill-tsp-white-dark)] rounded-lg cursor-pointer">
                <FileSearch class="text-[var(--icon-secondary)]" :size="18" />
              </button>
              <button @click="handleWorkspaceShow"
                class="h-8 px-2 sm:px-3 rounded-lg inline-flex items-center gap-1.5 hover:bg-[var(--fill-tsp-white-dark)] cursor-pointer border border-[var(--border-btn-main)] bg-[var(--background-white-main)]"
                :title="t('Open Manus workspace')">
                <Monitor class="text-[var(--icon-secondary)]" :size="18" />
                <span class="hidden sm:inline text-[var(--text-secondary)] text-sm font-medium whitespace-nowrap">{{ t('Workspace') }}</span>
              </button>
            </div>
          </div>
          <div class="w-full flex justify-between items-center">
          </div>
        </div>
        <div class="flex-1"></div>
      </div>
      <div class="mx-auto w-full max-w-full sm:max-w-[768px] sm:min-w-[390px] flex flex-col flex-1">
        <div class="flex flex-col w-full gap-[12px] pb-[80px] pt-[12px] flex-1 overflow-y-auto">
          <ChatMessage v-for="(message, index) in messages" :key="index" :message="message"
            :hideHeader="isConsecutiveAssistant(messages, index)"
            @toolClick="handleToolClick" />

          <!-- Loading indicator -->
          <LoadingIndicator v-if="isLoading && !streamStalled" :text="loadingText" />
          <div v-if="streamStalled" role="alert"
            class="rounded-xl border border-[var(--border-main)] bg-[var(--background-white-main)] px-4 py-3 text-sm text-[var(--text-secondary)]">
            {{ t('Live updates stopped while the task may still be running. Refresh this page or stop the task.') }}
          </div>
        </div>

        <div class="flex flex-col bg-[var(--background-gray-main)] sticky bottom-0">
          <button @click="handleFollow" v-if="!follow"
            class="flex items-center justify-center w-[36px] h-[36px] rounded-full bg-[var(--background-white-main)] hover:bg-[var(--background-gray-main)] clickable border border-[var(--border-main)] shadow-[0px_5px_16px_0px_var(--shadow-S),0px_0px_1.25px_0px_var(--shadow-S)] absolute -top-20 left-1/2 -translate-x-1/2">
            <ArrowDown class="text-[var(--icon-primary)]" :size="20" />
          </button>
          <PlanPanel v-if="plan && plan.steps.length > 0" :plan="plan" />
          <ChatBox v-model="inputMessage" v-model:attachments="attachments" :rows="1"
            :selected-model-id="selectedModelId" :model-options="modelOptions" show-model-picker
            model-picker-disabled @submit="handleSubmit" :isRunning="isLoading"
            :isStopping="isStopping" @stop="handleStop" />
        </div>
      </div>
    </div>
    <ToolPanel ref="toolPanel" :size="toolPanelSize" :sessionId="sessionId" :realTime="realTime" 
      :isShare="false"
      @jumpToRealTime="jumpToRealTime" />
  </SimpleBar>
</template>

<script setup lang="ts">
import SimpleBar from '../components/SimpleBar.vue';
import { computed, ref, onMounted, watch, nextTick, onUnmounted, reactive, toRefs } from 'vue';
import { useRouter, onBeforeRouteUpdate } from 'vue-router';
import { useI18n } from 'vue-i18n';
import ChatBox from '../components/ChatBox.vue';
import ChatMessage from '../components/ChatMessage.vue';
import * as agentApi from '../api/agent';
import { Message, MessageContent, ToolContent, AttachmentsContent, isConsecutiveAssistant } from '../types/message';
import { PlanEventData, AgentSSEEvent } from '../types/event';
import {
  getLatestTurnId,
  hasTerminalEventForLatestTurn,
  hasTerminalEventForTurn,
  useAgentEvents,
} from '../composables/useAgentEvents';
import ToolPanel from '../components/ToolPanel.vue'
import PlanPanel from '../components/PlanPanel.vue';
import { ArrowDown, FileSearch, Lock, Globe, Link, Check, Monitor } from 'lucide-vue-next';
import ShareIcon from '@/components/icons/ShareIcon.vue';
import { showErrorToast, showInfoToast, showSuccessToast } from '../utils/toast';
import type { FileInfo } from '../api/file';
import { useSessionFileList } from '../composables/useSessionFileList'
import { useFilePanel } from '../composables/useFilePanel'
import { copyToClipboard } from '../utils/dom'
import { SessionStatus } from '../types/response';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import LoadingIndicator from '@/components/ui/LoadingIndicator.vue';
import { getCachedClientConfig } from '@/api/config';
import {
  buildChatModelOptions,
  CURRENT_SESSION_MODEL_ID,
  resolveModelIdForConfig,
  SYSTEM_MODEL_ID,
  upsertCurrentSessionModelOption,
} from '@/api/agentConfig';
import type { ChatModelOption } from '@/api/agentConfig';
import type { SessionModelConfig } from '@/types/response';
import { createUuid } from '@/utils/uuid';

const router = useRouter()
const { t } = useI18n()
const { showSessionFileList } = useSessionFileList()
const { hideFilePanel } = useFilePanel()

// Create initial state factory
const createInitialState = () => ({
  inputMessage: '',
  isLoading: false,
  isStopping: false,
  streamStalled: false,
  retryAttempt: 0,
  sessionId: undefined as string | undefined,
  messages: [] as Message[],
  toolPanelSize: 0,
  realTime: true,
  follow: true,
  title: t('New Chat'),
  plan: undefined as PlanEventData | undefined,
  lastNoMessageTool: undefined as ToolContent | undefined,
  lastMessageTool: undefined as ToolContent | undefined,
  lastTool: undefined as ToolContent | undefined,
  lastEventId: undefined as string | undefined,
  cancelCurrentChat: null as (() => void) | null,
  attachments: [] as FileInfo[],
  shareMode: 'private' as 'private' | 'public', // Default to private mode
  linkCopied: false,
  sharingLoading: false // Loading state for share operations
});

// Create reactive state
const state = reactive(createInitialState());

// Destructure refs from reactive state
const {
  inputMessage,
  isLoading,
  isStopping,
  streamStalled,
  retryAttempt,
  sessionId,
  messages,
  toolPanelSize,
  realTime,
  follow,
  title,
  plan,
  lastNoMessageTool,
  lastTool,
  lastEventId,
  cancelCurrentChat,
  attachments,
  shareMode,
  linkCopied,
  sharingLoading
} = toRefs(state);

// Non-state refs that don't need reset
const toolPanel = ref<InstanceType<typeof ToolPanel>>()
const simpleBarRef = ref<InstanceType<typeof SimpleBar>>();
const observerRef = ref<HTMLDivElement>();
const chatContainerRef = ref<HTMLDivElement>();
const modelOptions = ref<ChatModelOption[]>([]);
const selectedModelId = ref(SYSTEM_MODEL_ID);
const loadingText = computed(() => (
  retryAttempt.value > 0
    ? `${t('Reconnecting')} (${retryAttempt.value})`
    : t('Thinking')
));

const loadModelOptions = async () => {
  modelOptions.value = buildChatModelOptions(await getCachedClientConfig());
};

const syncSelectedSessionModel = (modelConfig?: SessionModelConfig | null) => {
  const nextModelId = resolveModelIdForConfig(modelConfig, modelOptions.value);
  modelOptions.value = upsertCurrentSessionModelOption(
    modelOptions.value,
    nextModelId === CURRENT_SESSION_MODEL_ID ? modelConfig : null,
  );
  selectedModelId.value = nextModelId;
};

// Shared SSE event -> message list conversion
const { handleEvent, resetEventHistory } = useAgentEvents(
  { messages, title, plan, isLoading, lastEventId, lastTool, lastNoMessageTool },
  {
    onToolActivity: (tool: ToolContent) => {
      if (realTime.value) {
        toolPanel.value?.showToolPanel(tool, true);
      }
    },
  }
);

let sessionGeneration = 0;
let activeChatToken: symbol | null = null;
let chatWatchdogTimer: number | undefined;
let rearmCurrentChatWatchdog: (() => void) | null = null;
let componentUnmounted = false;

const CHAT_NO_PROGRESS_TIMEOUT_MS = 120_000;
const CHAT_MAX_STALLED_RECONNECTS = 3;

const clearChatWatchdog = () => {
  if (chatWatchdogTimer !== undefined) {
    window.clearTimeout(chatWatchdogTimer);
    chatWatchdogTimer = undefined;
  }
};

const isCurrentSession = (targetSessionId: string, generation: number) => (
  sessionGeneration === generation && sessionId.value === targetSessionId
);

// Reset all refs to their initial values
const resetState = () => {
  sessionGeneration += 1;
  activeChatToken = null;
  rearmCurrentChatWatchdog = null;
  clearChatWatchdog();
  // Cancel any existing chat connection
  if (cancelCurrentChat.value) {
    cancelCurrentChat.value();
  }

  resetEventHistory();

  // Reset reactive state to initial values
  Object.assign(state, createInitialState());
};

// Watch message changes and automatically scroll to bottom
watch(messages, async () => {
  await nextTick();
  if (follow.value) {
    simpleBarRef.value?.scrollToBottom();
  }
}, { deep: true });



const handleSubmit = () => {
  if (isLoading.value || isStopping.value) return;
  void chat(inputMessage.value, attachments.value);
}

const chat = async (
  message: string = '',
  files: FileInfo[] = [],
  resumedSubmissionId?: string,
) => {
  const targetSessionId = sessionId.value;
  if (!targetSessionId || isStopping.value) return;
  const generation = sessionGeneration;
  const chatToken = Symbol(targetSessionId);
  const hasNewSubmission = Boolean(message.trim()) || files.length > 0;
  const submissionId = resumedSubmissionId ?? (hasNewSubmission ? createUuid() : undefined);
  const attachmentPayload = files.map((file: FileInfo) => ({
    file_id: file.file_id,
    filename: file.filename,
  }));

  // Cancel any existing chat connection before starting a new one
  clearChatWatchdog();
  rearmCurrentChatWatchdog = null;
  if (cancelCurrentChat.value) {
    cancelCurrentChat.value();
    cancelCurrentChat.value = null;
  }
  activeChatToken = chatToken;

  const isActiveChat = () => (
    activeChatToken === chatToken
    && isCurrentSession(targetSessionId, generation)
  );

  let connectionSequence = 0;
  let progressVersion = 0;
  let stalledReconnects = 0;
  let lastProgressAt = Date.now();

  const isActiveConnection = (sequence: number) => (
    isActiveChat() && connectionSequence === sequence
  );

  const finishChat = (cancelTransport: boolean) => {
    if (!isActiveChat()) return;
    clearChatWatchdog();
    rearmCurrentChatWatchdog = null;
    const cancel = cancelCurrentChat.value;
    cancelCurrentChat.value = null;
    activeChatToken = null;
    isLoading.value = false;
    streamStalled.value = false;
    retryAttempt.value = 0;
    if (cancelTransport) {
      cancel?.();
    }
  };

  const failChatBeforeStreaming = (error: unknown) => {
    if (!isActiveChat()) return;
    console.error('Chat error:', error);
    clearChatWatchdog();
    rearmCurrentChatWatchdog = null;
    cancelCurrentChat.value = null;
    activeChatToken = null;
    isLoading.value = false;
    streamStalled.value = false;
    retryAttempt.value = 0;
    showErrorToast(t('Connection lost. Please retry.'));
  };

  const markStreamStalled = () => {
    if (!isActiveChat()) return;
    clearChatWatchdog();
    rearmCurrentChatWatchdog = null;
    const cancel = cancelCurrentChat.value;
    cancelCurrentChat.value = null;
    activeChatToken = null;
    // The durable task is still active. Keep submission blocked and the stop
    // control available, but remove the indefinite thinking animation.
    isLoading.value = true;
    streamStalled.value = true;
    retryAttempt.value = 0;
    cancel?.();
    showErrorToast(t(
      'Live updates stopped while the task may still be running. Refresh this page or stop the task.',
    ));
  };

  const isTerminalSnapshot = (events: AgentSSEEvent[]) => (
    submissionId
      ? hasTerminalEventForTurn(events, submissionId)
      : hasTerminalEventForLatestTurn(events)
  );

  const applyDurableSnapshot = (events: AgentSSEEvent[]): boolean => {
    let snapshotMadeProgress = false;
    for (const event of events) {
      const isCurrentUserInput = (
        submissionId
        && event.event === 'message'
        && (event.data as { role?: string }).role === 'user'
        && event.data.turn_id === submissionId
      );
      // A newly submitted turn is already rendered optimistically. The live
      // durable stream intentionally skips this event, so reconciliation must
      // not append a second copy from full session history.
      if (isCurrentUserInput) continue;
      if (handleEvent(event)) {
        snapshotMadeProgress = true;
        progressVersion += 1;
      }
    }
    return snapshotMadeProgress;
  };

  const reconcileTransportFailure = async (
    sequence: number,
    error: unknown,
  ) => {
    if (!isActiveConnection(sequence) || isStopping.value) return;
    console.error('Chat stream retries exhausted:', error);
    clearChatWatchdog();
    retryAttempt.value = 0;

    try {
      const latest = await agentApi.getSession(targetSessionId);
      if (!isActiveConnection(sequence) || isStopping.value) return;
      applyDurableSnapshot(latest.events);
      const isActiveStatus = (
        latest.status === SessionStatus.RUNNING
        || latest.status === SessionStatus.PENDING
      );
      if (isTerminalSnapshot(latest.events) || !isActiveStatus) {
        finishChat(true);
        return;
      }
    } catch (reconcileError) {
      console.error(
        'Failed to reconcile session after chat stream failure:',
        reconcileError,
      );
      if (!isActiveConnection(sequence) || isStopping.value) return;
    }

    // A POST may already have been durably accepted even when its stream can
    // no longer reconnect. Never unlock another submission without proving
    // the current turn terminal; keep the explicit stop control available.
    markStreamStalled();
  };

  const connectStream = async (includeSubmission: boolean): Promise<void> => {
    if (!isActiveChat() || isStopping.value) return;

    clearChatWatchdog();
    if (cancelCurrentChat.value) {
      cancelCurrentChat.value();
      cancelCurrentChat.value = null;
    }

    const sequence = ++connectionSequence;
    const armWatchdog = () => {
      if (
        !isActiveConnection(sequence)
        || isStopping.value
        || streamStalled.value
      ) {
        return;
      }
      clearChatWatchdog();
      const elapsed = Date.now() - lastProgressAt;
      const remaining = Math.max(0, CHAT_NO_PROGRESS_TIMEOUT_MS - elapsed);
      chatWatchdogTimer = window.setTimeout(() => {
        chatWatchdogTimer = undefined;
        void reconcileNoProgress(sequence, progressVersion);
      }, remaining);
    };
    rearmCurrentChatWatchdog = armWatchdog;

    try {
      const cancel = await agentApi.chatWithSession(
        targetSessionId,
        includeSubmission ? message : '',
        lastEventId.value,
        includeSubmission ? attachmentPayload : [],
        {
          onOpen: () => {
            if (!isActiveConnection(sequence)) return;
            retryAttempt.value = 0;
            armWatchdog();
          },
          onMessage: ({ event, data }) => {
            if (!isActiveConnection(sequence)) return false;
            const madeProgress = handleEvent({
              event: event as AgentSSEEvent['event'],
              data: data as AgentSSEEvent['data'],
            });
            if (madeProgress) {
              progressVersion += 1;
              stalledReconnects = 0;
              lastProgressAt = Date.now();
              streamStalled.value = false;
              armWatchdog();
            }
            return madeProgress;
          },
          onClose: () => {
            if (!isActiveConnection(sequence)) return;
            finishChat(false);
          },
          onError: (error) => {
            if (!isActiveConnection(sequence)) return;
            void reconcileTransportFailure(sequence, error);
          },
          onRetry: ({ attempt }) => {
            if (!isActiveConnection(sequence)) return;
            clearChatWatchdog();
            retryAttempt.value = attempt;
          },
        },
        submissionId,
      );
      if (!isActiveConnection(sequence)) {
        cancel();
        return;
      }
      cancelCurrentChat.value = cancel;
      armWatchdog();
    } catch (error) {
      if (!isActiveConnection(sequence)) return;
      failChatBeforeStreaming(error);
    }
  };

  const recoverActiveStream = async (sequence: number, madeProgress: boolean) => {
    if (!isActiveConnection(sequence) || isStopping.value) return;
    if (madeProgress) {
      stalledReconnects = 0;
    } else {
      stalledReconnects += 1;
    }
    if (stalledReconnects > CHAT_MAX_STALLED_RECONNECTS) {
      markStreamStalled();
      return;
    }
    isLoading.value = true;
    streamStalled.value = false;
    retryAttempt.value = stalledReconnects;
    // Give the next connection a full observation window. This is recovery
    // progress, not task progress, so it does not reset stalledReconnects.
    lastProgressAt = Date.now();
    await connectStream(false);
  };

  const reconcileNoProgress = async (
    sequence: number,
    observedProgressVersion: number,
  ) => {
    if (!isActiveConnection(sequence) || isStopping.value) return;

    try {
      const latest = await agentApi.getSession(targetSessionId);
      if (
        !isActiveConnection(sequence)
        || isStopping.value
        || progressVersion !== observedProgressVersion
      ) {
        return;
      }

      const snapshotMadeProgress = applyDurableSnapshot(latest.events);

      const isActiveStatus = (
        latest.status === SessionStatus.RUNNING
        || latest.status === SessionStatus.PENDING
      );
      if (isTerminalSnapshot(latest.events) || !isActiveStatus) {
        finishChat(true);
        return;
      }

      // Older terminal events in full session history are not terminal for the
      // active durable turn.
      isLoading.value = true;
      if (snapshotMadeProgress) {
        lastProgressAt = Date.now();
      }
      await recoverActiveStream(sequence, snapshotMadeProgress);
    } catch (error) {
      console.error('Failed to reconcile stalled chat stream:', error);
      if (
        !isActiveConnection(sequence)
        || isStopping.value
        || progressVersion !== observedProgressVersion
      ) {
        return;
      }
      await recoverActiveStream(sequence, false);
    }
  };

  if (message.trim()) {
    // Add user message to conversation list
    messages.value.push({
      type: 'user',
      content: {
        content: message,
        timestamp: Math.floor(Date.now() / 1000)
      } as MessageContent,
    });
  }

  if (files.length > 0) {
    messages.value.push({
      type: 'attachments',
      content: {
        role: 'user',
        attachments: files
      } as AttachmentsContent,
    });
  }

  // Automatically enable follow mode when sending message
  follow.value = true;

  // Clear input field and attachments
  inputMessage.value = '';
  attachments.value = [];
  isLoading.value = true;
  streamStalled.value = false;
  retryAttempt.value = 0;
  await connectStream(hasNewSubmission);
}

const restoreSession = async () => {
  const targetSessionId = sessionId.value;
  if (!targetSessionId) {
    showErrorToast(t('Session not found'));
    return;
  }
  const generation = sessionGeneration;
  try {
    const session = await agentApi.getSession(targetSessionId);
    if (!isCurrentSession(targetSessionId, generation)) return;
    syncSelectedSessionModel(session.model_config);
    // Initialize share mode based on session state
    shareMode.value = session.is_shared ? 'public' : 'private';
    realTime.value = false;
    for (const event of session.events) {
      handleEvent(event);
    }
    realTime.value = true;
    const hasTerminalEvent = hasTerminalEventForLatestTurn(session.events);
    if (
      (session.status === SessionStatus.RUNNING || session.status === SessionStatus.PENDING)
      && !hasTerminalEvent
    ) {
      await chat('', [], getLatestTurnId(session.events));
    } else {
      isLoading.value = false;
      streamStalled.value = false;
    }
    void agentApi.clearUnreadMessageCount(targetSessionId).catch((error) => {
      console.error('Failed to clear unread message count:', error);
    });
  } catch (error) {
    if (!isCurrentSession(targetSessionId, generation)) return;
    console.error('Failed to restore session:', error);
    isLoading.value = false;
    streamStalled.value = false;
    retryAttempt.value = 0;
    showErrorToast(t('Failed to restore session. Please retry.'));
  }
}



onBeforeRouteUpdate((to, _, next) => {
  toolPanel.value?.hideToolPanel();
  hideFilePanel();
  resetState();
  if (to.params.sessionId) {
    messages.value = [];
    sessionId.value = String(to.params.sessionId) as string;
    restoreSession();
  }
  next();
})

// Initialize active conversation
onMounted(async () => {
  const initializationGeneration = sessionGeneration;
  hideFilePanel();
  const initialModelId = history.state?.modelId;
  await loadModelOptions();
  if (componentUnmounted || sessionGeneration !== initializationGeneration) return;
  if (typeof initialModelId === 'string') {
    selectedModelId.value = initialModelId;
  }
  const routeParams = router.currentRoute.value.params;
  if (routeParams.sessionId) {
    // If sessionId is included in URL, use it directly
    sessionId.value = String(routeParams.sessionId) as string;
    // Get initial message from history.state
    const message = history.state?.message;
    const files: FileInfo[] = history.state?.files ?? [];
    history.replaceState({}, document.title);
    if (message || files.length > 0) {
      chat(message ?? '', files);
    } else {
      restoreSession();
    }
  }


});

onUnmounted(() => {
  componentUnmounted = true;
  sessionGeneration += 1;
  activeChatToken = null;
  rearmCurrentChatWatchdog = null;
  clearChatWatchdog();
  if (cancelCurrentChat.value) {
    cancelCurrentChat.value();
    cancelCurrentChat.value = null;
  }
})

const isLastNoMessageTool = (tool: ToolContent) => {
  return tool.tool_call_id === lastNoMessageTool.value?.tool_call_id;
}

const isLiveTool = (tool: ToolContent) => {
  if (tool.status === 'calling') {
    return true;
  }
  if (!isLastNoMessageTool(tool)) {
    return false;
  }
  if (tool.timestamp > Math.floor(Date.now() / 1000) - 5 * 60) {
    return true;
  }
  return false;
}

const handleToolClick = (tool: ToolContent) => {
  realTime.value = false;
  if (sessionId.value) {
    toolPanel.value?.showToolPanel(tool, isLiveTool(tool));
  }
}

const jumpToRealTime = () => {
  realTime.value = true;
  if (lastNoMessageTool.value) {
    toolPanel.value?.showToolPanel(lastNoMessageTool.value, isLiveTool(lastNoMessageTool.value));
  }
}

const handleFollow = () => {
  follow.value = true;
  simpleBarRef.value?.scrollToBottom();
}

const handleScroll = (_: Event) => {
  follow.value = simpleBarRef.value?.isScrolledToBottom() ?? false;
}

const handleStop = async () => {
  const targetSessionId = sessionId.value;
  if (targetSessionId && !isStopping.value) {
    const generation = sessionGeneration;
    isStopping.value = true;
    clearChatWatchdog();
    try {
      await agentApi.stopSession(targetSessionId);
      if (!isCurrentSession(targetSessionId, generation)) return;
      activeChatToken = null;
      rearmCurrentChatWatchdog = null;
      cancelCurrentChat.value?.();
      cancelCurrentChat.value = null;
      isLoading.value = false;
      streamStalled.value = false;
      retryAttempt.value = 0;
    } catch (error) {
      console.error('Failed to stop session:', error);
      if (!isCurrentSession(targetSessionId, generation)) return;
      // The backend intentionally returns 409/503 when task cancellation may
      // have succeeded but sandbox process cleanup was not confirmed. Session
      // status or a terminal turn cannot prove that shell descendants are
      // gone, so every failed stop remains explicitly retryable.
      activeChatToken = null;
      rearmCurrentChatWatchdog = null;
      cancelCurrentChat.value?.();
      cancelCurrentChat.value = null;
      isLoading.value = true;
      streamStalled.value = true;
      retryAttempt.value = 0;
      showErrorToast(t('Failed to stop task. It may still be running; please try again.'));
    } finally {
      if (isCurrentSession(targetSessionId, generation)) {
        isStopping.value = false;
        if (isLoading.value && !streamStalled.value) {
          rearmCurrentChatWatchdog?.();
        }
      }
    }
  }
}

const handleFileListShow = () => {
  showSessionFileList()
}

const handleWorkspaceShow = () => {
  if (!lastNoMessageTool.value) {
    showInfoToast(t('No Manus workspace yet'));
    return;
  }
  const live = isLiveTool(lastNoMessageTool.value);
  realTime.value = live;
  toolPanel.value?.showToolPanel(lastNoMessageTool.value, live);
}

// Share functionality handlers
const handleShareModeChange = async (mode: 'private' | 'public') => {
  if (!sessionId.value || sharingLoading.value) return;
  
  // If mode is same as current, no need to call API
  if (shareMode.value === mode) {
    linkCopied.value = false;
    return;
  }
  
  try {
    sharingLoading.value = true;
    
    if (mode === 'public') {
      await agentApi.shareSession(sessionId.value);
    } else {
      await agentApi.unshareSession(sessionId.value);
    }
    
    shareMode.value = mode;
    linkCopied.value = false;
  } catch (error) {
    console.error('Error changing share mode:', error);
    showErrorToast(t('Failed to change sharing settings'));
  } finally {
    sharingLoading.value = false;
  }
}

const handleInstantShare = async () => {
  if (!sessionId.value) return;
  
  try {
    sharingLoading.value = true;
    await agentApi.shareSession(sessionId.value);
    shareMode.value = 'public';
    linkCopied.value = false;
  } catch (error) {
    console.error('Error sharing session:', error);
    showErrorToast(t('Failed to share session'));
  } finally {
    sharingLoading.value = false;
  }
}

const handleCopyLink = async () => {
  if (!sessionId.value) return;
  
  const shareUrl = `${window.location.origin}/share/${sessionId.value}`;
  
  try {
    const success = await copyToClipboard(shareUrl);
    
    if (success) {
      linkCopied.value = true;
      setTimeout(() => {
        linkCopied.value = false;
      }, 3000);
      showSuccessToast(t('Link copied to clipboard'));
    } else {
      showErrorToast(t('Failed to copy link'));
    }
  } catch (error) {
    console.error('Error copying share link:', error);
    showErrorToast(t('Failed to copy link'));
  }
}
</script>
