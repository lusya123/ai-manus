import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { defineComponent, nextTick } from 'vue';
import { flushPromises, shallowMount } from '@vue/test-utils';
import ChatPage from '../ChatPage.vue';
import { i18n } from '../../composables/useI18n';
import { SessionStatus } from '../../types/response';
import type { SSECallbacks } from '../../api/client';
import type { AgentSSEEvent } from '../../types/event';

const { currentRoute, agentMocks, toastMocks } = vi.hoisted(() => ({
  currentRoute: {
    value: { params: { sessionId: 'session-1' } },
  },
  agentMocks: {
    getSession: vi.fn(),
    chatWithSession: vi.fn(),
    clearUnreadMessageCount: vi.fn(),
    stopSession: vi.fn(),
    shareSession: vi.fn(),
    unshareSession: vi.fn(),
  },
  toastMocks: {
    showErrorToast: vi.fn(),
    showInfoToast: vi.fn(),
    showSuccessToast: vi.fn(),
  },
}));

vi.mock('vue-router', () => ({
  useRouter: () => ({ currentRoute }),
  onBeforeRouteUpdate: vi.fn(),
}));

vi.mock('../../api/agent', () => agentMocks);

vi.mock('../../api/config', () => ({
  getCachedClientConfig: vi.fn().mockResolvedValue(null),
}));

vi.mock('../../utils/toast', () => toastMocks);

const ChatBoxStub = defineComponent({
  name: 'ChatBox',
  props: {
    modelValue: { type: String, default: '' },
    attachments: { type: Array, default: () => [] },
    isRunning: { type: Boolean, default: false },
    isStopping: { type: Boolean, default: false },
  },
  emits: ['update:modelValue', 'update:attachments', 'submit', 'stop'],
  template: `
    <div>
      <button data-testid="submit" @click="$emit('submit')">submit</button>
      <button data-testid="stop" @click="$emit('stop')">stop</button>
    </div>
  `,
});

const SimpleBarStub = defineComponent({
  name: 'SimpleBar',
  methods: {
    scrollToBottom() {},
    isScrolledToBottom() {
      return true;
    },
  },
  template: '<div><slot /></div>',
});

const completedSession = () => ({
  session_id: 'session-1',
  title: 'Session',
  status: SessionStatus.COMPLETED,
  events: [],
  is_shared: false,
});

const runningSession = () => ({
  ...completedSession(),
  status: SessionStatus.RUNNING,
  events: [{
    event: 'message',
    data: {
      event_id: 'user-1',
      turn_id: 'turn-1',
      role: 'user',
      content: 'run',
      attachments: [],
      timestamp: 1,
    },
  }] as AgentSSEEvent[],
});

const activeSession = (events: AgentSSEEvent[] = []) => ({
  ...completedSession(),
  status: SessionStatus.RUNNING,
  events,
});

const CHAT_NO_PROGRESS_TIMEOUT_MS = 120_000;

const mountPage = async () => {
  const wrapper = shallowMount(ChatPage, {
    global: {
      plugins: [i18n],
      stubs: {
        ChatBox: ChatBoxStub,
        SimpleBar: SimpleBarStub,
      },
    },
  });
  await flushPromises();
  return wrapper;
};

const currentChatCallbacks = () => (
  agentMocks.chatWithSession.mock.calls[
    agentMocks.chatWithSession.mock.calls.length - 1
  ]?.[4] as
    | SSECallbacks<AgentSSEEvent['data']>
    | undefined
);

describe('ChatPage chat lifecycle', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState({}, '', '/chat/session-1');
    currentRoute.value = { params: { sessionId: 'session-1' } };
    Object.values(agentMocks).forEach(mock => mock.mockReset());
    Object.values(toastMocks).forEach(mock => mock.mockReset());
    agentMocks.getSession.mockResolvedValue(completedSession());
    agentMocks.clearUnreadMessageCount.mockResolvedValue(undefined);
    agentMocks.chatWithSession.mockResolvedValue(vi.fn());
    agentMocks.stopSession.mockResolvedValue(undefined);
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('blocks a second submission while the active turn is running', async () => {
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);

    chatBox.vm.$emit('update:modelValue', 'first task');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    expect(agentMocks.chatWithSession).toHaveBeenCalledOnce();
    expect(chatBox.props('isRunning')).toBe(true);

    chatBox.vm.$emit('update:modelValue', 'must stay a draft');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    expect(agentMocks.chatWithSession).toHaveBeenCalledOnce();
    wrapper.unmount();
  });

  it('keeps files and one submission id for a files-only initial send', async () => {
    const file = {
      file_id: 'file-only-1',
      filename: 'report.pdf',
      size: 10,
      upload_date: '2026-01-01T00:00:00Z',
    };
    window.history.replaceState({ files: [file] }, '', '/chat/session-1');

    const wrapper = await mountPage();

    expect(agentMocks.getSession).not.toHaveBeenCalled();
    expect(agentMocks.chatWithSession).toHaveBeenCalledOnce();
    const call = agentMocks.chatWithSession.mock.calls[0]!;
    expect(call[1]).toBe('');
    expect(call[3]).toEqual([
      { file_id: 'file-only-1', filename: 'report.pdf' },
    ]);
    expect(call[5]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    wrapper.unmount();
  });

  it('keeps loading terminal after reconnect callbacks and a duplicate terminal event', async () => {
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'finish once');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();
    const callbacks = currentChatCallbacks();
    expect(callbacks).toBeDefined();

    callbacks?.onMessage?.({
      event: 'done',
      data: {
        event_id: 'done-1',
        turn_id: 'turn-1',
        timestamp: 1,
      },
    });
    await nextTick();
    expect(chatBox.props('isRunning')).toBe(false);

    callbacks?.onOpen?.();
    callbacks?.onMessage?.({
      event: 'done',
      data: {
        event_id: 'done-1',
        turn_id: 'turn-1',
        timestamp: 1,
      },
    });
    await nextTick();
    expect(chatBox.props('isRunning')).toBe(false);
    wrapper.unmount();
  });

  it('reconciles an open stream with no business events and reconnects the same durable turn', async () => {
    vi.useFakeTimers();
    const cancelFunctions: Array<ReturnType<typeof vi.fn>> = [];
    agentMocks.chatWithSession.mockImplementation(async () => {
      const cancel = vi.fn();
      cancelFunctions.push(cancel);
      return cancel;
    });
    agentMocks.getSession
      .mockResolvedValueOnce(completedSession())
      .mockResolvedValue(activeSession());

    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'long silent task');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    const firstCall = agentMocks.chatWithSession.mock.calls[0]!;
    const submissionId = firstCall[5] as string;
    currentChatCallbacks()?.onOpen?.();
    currentChatCallbacks()?.onMessage?.({
      event: 'accepted',
      data: {
        event_id: 'accepted-1',
        transport_cursor: '1782506372223-0',
        turn_id: submissionId,
        submission_id: submissionId,
        state: 'running',
        timestamp: 1,
      },
    });

    // Built-in SSE comment pings are filtered by createSSEConnection and do not
    // call this page callback, so an otherwise open reader reaches the watchdog.
    await vi.advanceTimersByTimeAsync(CHAT_NO_PROGRESS_TIMEOUT_MS);
    await flushPromises();

    expect(agentMocks.getSession).toHaveBeenCalledTimes(2);
    expect(agentMocks.chatWithSession).toHaveBeenCalledTimes(2);
    const reconnectCall = agentMocks.chatWithSession.mock.calls[1]!;
    expect(reconnectCall[1]).toBe('');
    expect(reconnectCall[2]).toBe('1782506372223-0');
    expect(reconnectCall[3]).toEqual([]);
    expect(reconnectCall[5]).toBe(submissionId);
    expect(cancelFunctions[0]).toHaveBeenCalledOnce();
    expect(chatBox.props('isRunning')).toBe(true);
    wrapper.unmount();
  });

  it('finishes from durable history when watchdog reconciliation finds the turn terminal', async () => {
    vi.useFakeTimers();
    const cancel = vi.fn();
    agentMocks.chatWithSession.mockResolvedValue(cancel);
    agentMocks.getSession.mockResolvedValueOnce(completedSession());

    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'finish durably');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    const submissionId = agentMocks.chatWithSession.mock.calls[0]![5] as string;
    agentMocks.getSession.mockResolvedValue(activeSession([
      {
        event: 'message',
        data: {
          event_id: 'user-durable',
          turn_id: submissionId,
          role: 'user',
          content: 'finish durably',
          attachments: [],
          timestamp: 1,
        },
      },
      {
        event: 'message',
        data: {
          event_id: 'assistant-durable',
          turn_id: submissionId,
          role: 'assistant',
          content: 'finished',
          attachments: [],
          timestamp: 2,
        },
      },
      {
        event: 'done',
        data: {
          event_id: 'done-durable',
          turn_id: submissionId,
          timestamp: 3,
        },
      },
    ] as AgentSSEEvent[]));

    currentChatCallbacks()?.onOpen?.();
    await vi.advanceTimersByTimeAsync(CHAT_NO_PROGRESS_TIMEOUT_MS);
    await flushPromises();

    expect(agentMocks.chatWithSession).toHaveBeenCalledOnce();
    expect(cancel).toHaveBeenCalledOnce();
    expect(chatBox.props('isRunning')).toBe(false);
    expect(wrapper.find('[role="alert"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('bounds consecutive silent reconnects and leaves a stoppable task without a spinner', async () => {
    vi.useFakeTimers();
    agentMocks.getSession
      .mockResolvedValueOnce(completedSession())
      .mockResolvedValue(activeSession());

    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'never emits progress');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();
    currentChatCallbacks()?.onOpen?.();

    for (let cycle = 0; cycle < 4; cycle += 1) {
      await vi.advanceTimersByTimeAsync(CHAT_NO_PROGRESS_TIMEOUT_MS);
      await flushPromises();
      currentChatCallbacks()?.onOpen?.();
    }
    await nextTick();

    expect(agentMocks.getSession).toHaveBeenCalledTimes(5);
    expect(agentMocks.chatWithSession).toHaveBeenCalledTimes(4);
    expect(wrapper.find('[role="alert"]').text()).toContain('Live updates stopped');
    expect(chatBox.props('isRunning')).toBe(true);
    expect(toastMocks.showErrorToast).toHaveBeenCalledOnce();
    wrapper.unmount();
  });

  it('new business progress resets the consecutive silent-reconnect budget', async () => {
    vi.useFakeTimers();
    agentMocks.getSession
      .mockResolvedValueOnce(completedSession())
      .mockResolvedValue(activeSession());

    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'eventually reports progress');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    for (let cycle = 0; cycle < 3; cycle += 1) {
      await vi.advanceTimersByTimeAsync(CHAT_NO_PROGRESS_TIMEOUT_MS);
      await flushPromises();
    }
    currentChatCallbacks()?.onMessage?.({
      event: 'title',
      data: {
        event_id: 'new-title-progress',
        title: 'Still working',
        timestamp: 2,
      },
    });

    for (let cycle = 0; cycle < 3; cycle += 1) {
      await vi.advanceTimersByTimeAsync(CHAT_NO_PROGRESS_TIMEOUT_MS);
      await flushPromises();
    }
    await nextTick();

    expect(wrapper.find('[role="alert"]').exists()).toBe(false);
    expect(chatBox.props('isRunning')).toBe(true);
    expect(toastMocks.showErrorToast).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it('clears the no-progress watchdog on unmount', async () => {
    vi.useFakeTimers();
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'leave this page');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    wrapper.unmount();
    await vi.advanceTimersByTimeAsync(CHAT_NO_PROGRESS_TIMEOUT_MS);
    await flushPromises();

    expect(agentMocks.getSession).toHaveBeenCalledOnce();
  });

  it('clears the no-progress watchdog after a successful stop', async () => {
    vi.useFakeTimers();
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'stop before watchdog');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    chatBox.vm.$emit('stop');
    await flushPromises();
    await vi.advanceTimersByTimeAsync(CHAT_NO_PROGRESS_TIMEOUT_MS);
    await flushPromises();

    expect(agentMocks.stopSession).toHaveBeenCalledOnce();
    expect(agentMocks.getSession).toHaveBeenCalledOnce();
    expect(chatBox.props('isRunning')).toBe(false);
    wrapper.unmount();
  });

  it('prevents repeated stop requests and surfaces a failed stop after reconciliation', async () => {
    let rejectStop!: (error: Error) => void;
    const stopPending = new Promise<void>((_resolve, reject) => {
      rejectStop = reject;
    });
    agentMocks.stopSession.mockReturnValue(stopPending);
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'long task');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    chatBox.vm.$emit('stop');
    await nextTick();
    expect(chatBox.props('isStopping')).toBe(true);
    chatBox.vm.$emit('stop');
    expect(agentMocks.stopSession).toHaveBeenCalledOnce();

    // A durable terminal event can arrive after task cancellation but before
    // sandbox shell cleanup finishes. It must not turn a failed stop into a
    // false success or hide the retry control.
    currentChatCallbacks()?.onMessage?.({
      event: 'done',
      data: {
        event_id: 'done-before-cleanup',
        turn_id: 'turn-1',
        timestamp: 1,
      },
    });
    rejectStop(new Error('stop timeout'));
    await flushPromises();

    expect(agentMocks.getSession).toHaveBeenCalledOnce();
    expect(chatBox.props('isStopping')).toBe(false);
    expect(chatBox.props('isRunning')).toBe(true);
    expect(wrapper.find('[role="alert"]').exists()).toBe(true);
    expect(toastMocks.showErrorToast).toHaveBeenCalledOnce();

    agentMocks.stopSession.mockResolvedValue(undefined);
    chatBox.vm.$emit('stop');
    await flushPromises();
    expect(agentMocks.stopSession).toHaveBeenCalledTimes(2);
    expect(chatBox.props('isRunning')).toBe(false);
    wrapper.unmount();
  });

  it('reconciles exhausted stream retries and keeps an active task stoppable', async () => {
    agentMocks.getSession
      .mockResolvedValueOnce(completedSession())
      .mockResolvedValueOnce(runningSession());
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'network task');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    currentChatCallbacks()?.onError?.(new Error('retry limit exceeded'));
    await flushPromises();

    expect(agentMocks.getSession).toHaveBeenCalledTimes(2);
    expect(chatBox.props('isRunning')).toBe(true);
    expect(wrapper.find('[role="alert"]').exists()).toBe(true);
    expect(toastMocks.showErrorToast).toHaveBeenCalledOnce();

    chatBox.vm.$emit('update:modelValue', 'must not duplicate the active task');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();
    expect(agentMocks.chatWithSession).toHaveBeenCalledOnce();
    wrapper.unmount();
  });

  it('unlocks after exhausted stream retries only when durable state is terminal', async () => {
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'network task that finished');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    const submissionId = agentMocks.chatWithSession.mock.calls[0]![5] as string;
    agentMocks.getSession.mockResolvedValue({
      ...completedSession(),
      events: [
        {
          event: 'message',
          data: {
            event_id: 'user-network-terminal',
            turn_id: submissionId,
            role: 'user',
            content: 'network task that finished',
            attachments: [],
            timestamp: 1,
          },
        },
        {
          event: 'done',
          data: {
            event_id: 'done-network-terminal',
            turn_id: submissionId,
            timestamp: 2,
          },
        },
      ] as AgentSSEEvent[],
    });

    currentChatCallbacks()?.onError?.(new Error('retry limit exceeded'));
    await flushPromises();

    expect(chatBox.props('isRunning')).toBe(false);
    expect(wrapper.find('[role="alert"]').exists()).toBe(false);
    expect(toastMocks.showErrorToast).not.toHaveBeenCalled();
    wrapper.unmount();
  });
});
