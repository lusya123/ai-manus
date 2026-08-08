import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { defineComponent, nextTick } from 'vue';
import { flushPromises, shallowMount } from '@vue/test-utils';
import ChatPage from '../ChatPage.vue';
import { i18n } from '../../composables/useI18n';
import { SessionStatus } from '../../types/response';
import type { ChatStreamCallbacks } from '../../api/agent';
import { ChatSubmissionError } from '../../api/chatWs';
import type { AgentEvent } from '../../types/event';

const { currentRoute, routerPush, agentMocks, toastMocks } = vi.hoisted(() => ({
  currentRoute: {
    value: {
      path: '/chat/session-1',
      params: { sessionId: 'session-1' },
    },
  },
  routerPush: vi.fn(),
  agentMocks: {
    getSession: vi.fn(),
    chatWithSession: vi.fn(),
    leaveChatSession: vi.fn(),
    clearUnreadMessageCount: vi.fn(),
    stopSession: vi.fn(),
    shareSession: vi.fn(),
    unshareSession: vi.fn(),
    updateSessionTaskMode: vi.fn(),
    updateSessionTitle: vi.fn(),
    pinSession: vi.fn(),
    favoriteSession: vi.fn(),
    unfavoriteSession: vi.fn(),
    moveSessionProject: vi.fn(),
    deleteSession: vi.fn(),
  },
  toastMocks: {
    showErrorToast: vi.fn(),
    showInfoToast: vi.fn(),
    showSuccessToast: vi.fn(),
  },
}));

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal<typeof import('vue-router')>();
  return {
    ...actual,
    useRouter: () => ({ currentRoute, push: routerPush }),
    onBeforeRouteUpdate: vi.fn(),
  };
});

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

const computerPanelShow = vi.fn();
const ComputerPanelStub = defineComponent({
  name: 'ComputerPanel',
  setup(_props, { expose }) {
    expose({ showComputerPanel: computerPanelShow });
  },
  template: '<div data-testid="computer-panel" />',
});

const completedSession = (events: AgentEvent[] = []) => ({
  session_id: 'session-1',
  title: 'Session',
  status: SessionStatus.COMPLETED,
  events,
  is_shared: false,
  is_favorite: false,
  is_pinned: false,
  project_id: null,
  task_mode: 'agent' as const,
  model_config: null,
});

const runningSession = (events: AgentEvent[] = []) => ({
  ...completedSession(events),
  status: SessionStatus.RUNNING,
});

const mountPage = async () => {
  const wrapper = shallowMount(ChatPage, {
    global: {
      plugins: [i18n],
      stubs: {
        ChatBox: ChatBoxStub,
        SimpleBar: SimpleBarStub,
        ComputerPanel: ComputerPanelStub,
      },
    },
  });
  await flushPromises();
  return wrapper;
};

const currentChatCallbacks = (): ChatStreamCallbacks | undefined => (
  agentMocks.chatWithSession.mock.calls[
    agentMocks.chatWithSession.mock.calls.length - 1
  ]?.[4] as ChatStreamCallbacks | undefined
);

const submittedCalls = () => agentMocks.chatWithSession.mock.calls.filter(
  call => Boolean(call[1]) || (Array.isArray(call[3]) && call[3].length > 0),
);

describe('ChatPage WebSocket lifecycle', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState({}, '', '/chat/session-1');
    currentRoute.value = {
      path: '/chat/session-1',
      params: { sessionId: 'session-1' },
    };
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(() => false),
    }));
    Object.values(agentMocks).forEach(mock => mock.mockReset());
    Object.values(toastMocks).forEach(mock => mock.mockReset());
    computerPanelShow.mockReset();
    routerPush.mockReset();
    agentMocks.getSession.mockResolvedValue(completedSession());
    agentMocks.clearUnreadMessageCount.mockResolvedValue(undefined);
    agentMocks.chatWithSession.mockResolvedValue(vi.fn());
    agentMocks.leaveChatSession.mockResolvedValue(undefined);
    agentMocks.stopSession.mockResolvedValue(undefined);
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('hydrates a completed session and joins its WebSocket channel', async () => {
    const wrapper = await mountPage();

    expect(agentMocks.getSession).toHaveBeenCalledWith('session-1');
    expect(agentMocks.chatWithSession).toHaveBeenCalledWith(
      'session-1',
      '',
      undefined,
      undefined,
      expect.any(Object),
    );
    expect(wrapper.getComponent(ChatBoxStub).props('isRunning')).toBe(false);
    wrapper.unmount();
  });

  it('shows an informative toast when no Manus workspace exists yet', async () => {
    const wrapper = await mountPage();

    await wrapper.get('[data-testid="workspace-button"]').trigger('click');

    expect(toastMocks.showInfoToast).toHaveBeenCalledWith(
      i18n.global.t('No Manus workspace yet'),
    );
    expect(computerPanelShow).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it('reopens a legacy website turn as an interactive local preview', async () => {
    agentMocks.getSession.mockResolvedValue(completedSession([
      {
        event: 'message',
        data: {
          event_id: 'user-1',
          turn_id: 'turn-1',
          role: 'user',
          content: 'Build the website',
          attachments: [],
          timestamp: 1,
        },
      },
      {
        event: 'tool',
        data: {
          event_id: 'browser-1',
          turn_id: 'turn-1',
          tool_call_id: 'browser-1',
          name: 'browser',
          function: 'browser_navigate',
          args: { url: 'http://localhost:8099/index.html' },
          content: { screenshot: '' },
          status: 'called',
          timestamp: 2,
        },
      },
    ] as AgentEvent[]));
    const wrapper = await mountPage();

    await wrapper.get('[data-testid="workspace-button"]').trigger('click');

    expect(computerPanelShow).toHaveBeenCalledWith(
      expect.objectContaining({
        tool_call_id: 'browser-1',
        name: 'preview',
        function: 'preview_show',
        content: expect.objectContaining({ url: 'http://localhost:8099/index.html' }),
      }),
      false,
    );
    wrapper.unmount();
  });

  it('keeps the current-turn explicit preview ahead of later tool activity', async () => {
    agentMocks.getSession.mockResolvedValue(completedSession([
      {
        event: 'message',
        data: {
          event_id: 'user-1', turn_id: 'turn-1', role: 'user', content: 'Build it', attachments: [], timestamp: 1,
        },
      },
      {
        event: 'tool',
        data: {
          event_id: 'preview-1', turn_id: 'turn-1', tool_call_id: 'preview-1', name: 'preview', function: 'preview_show',
          args: { url: 'http://localhost:4173/' }, content: { url: 'http://localhost:4173/' }, status: 'called', timestamp: 2,
        },
      },
      {
        event: 'tool',
        data: {
          event_id: 'file-1', turn_id: 'turn-1', tool_call_id: 'file-1', name: 'file', function: 'file_read',
          args: { file: '/home/ubuntu/index.html' }, status: 'called', timestamp: 3,
        },
      },
    ] as AgentEvent[]));
    const wrapper = await mountPage();

    await wrapper.get('[data-testid="workspace-button"]').trigger('click');

    expect(computerPanelShow).toHaveBeenCalledWith(
      expect.objectContaining({ tool_call_id: 'preview-1', name: 'preview' }),
      false,
    );
    wrapper.unmount();
  });

  it('does not let later live tool events replace an interactive preview', async () => {
    const wrapper = await mountPage();
    const callbacks = currentChatCallbacks();
    callbacks?.onMessage?.({
      event: 'message',
      data: {
        event_id: 'user-live', turn_id: 'turn-live', role: 'user', content: 'Build it', attachments: [], timestamp: 1,
      },
    });
    callbacks?.onMessage?.({
      event: 'tool',
      data: {
        event_id: 'preview-live', turn_id: 'turn-live', tool_call_id: 'preview-live', name: 'preview', function: 'preview_show',
        args: { url: 'http://localhost:4173/' }, content: { url: 'http://localhost:4173/' }, status: 'called', timestamp: 2,
      },
    });
    callbacks?.onMessage?.({
      event: 'tool',
      data: {
        event_id: 'file-live', turn_id: 'turn-live', tool_call_id: 'file-live', name: 'file', function: 'file_read',
        args: { file: '/home/ubuntu/index.html' }, status: 'called', timestamp: 3,
      },
    });
    await nextTick();

    expect(computerPanelShow).toHaveBeenLastCalledWith(
      expect.objectContaining({ tool_call_id: 'preview-live', name: 'preview' }),
      false,
    );
    wrapper.unmount();
  });

  it('does not pin a derived localhost preview over newer live browser activity', async () => {
    const wrapper = await mountPage();
    const callbacks = currentChatCallbacks();
    callbacks?.onMessage?.({
      event: 'message',
      data: {
        event_id: 'user-local', turn_id: 'turn-local', role: 'user', content: 'Inspect local admin', attachments: [], timestamp: 1,
      },
    });
    callbacks?.onMessage?.({
      event: 'tool',
      data: {
        event_id: 'navigate-local', turn_id: 'turn-local', tool_call_id: 'navigate-local', name: 'browser', function: 'browser_navigate',
        args: { url: 'http://localhost:4173/admin' }, content: { screenshot: '/signed/shot-1' }, status: 'called', timestamp: 2,
      },
    });
    callbacks?.onMessage?.({
      event: 'tool',
      data: {
        event_id: 'click-local', turn_id: 'turn-local', tool_call_id: 'click-local', name: 'browser', function: 'browser_click',
        args: { element: 'Login' }, status: 'calling', timestamp: 3,
      },
    });
    await nextTick();

    expect(computerPanelShow).toHaveBeenLastCalledWith(
      expect.objectContaining({ tool_call_id: 'click-local', name: 'browser' }),
      true,
    );
    wrapper.unmount();
  });

  it('does not reuse an older-turn preview for the current browser task', async () => {
    agentMocks.getSession.mockResolvedValue(completedSession([
      {
        event: 'message',
        data: {
          event_id: 'user-1', turn_id: 'turn-1', role: 'user', content: 'Build it', attachments: [], timestamp: 1,
        },
      },
      {
        event: 'tool',
        data: {
          event_id: 'preview-1', turn_id: 'turn-1', tool_call_id: 'preview-1', name: 'preview', function: 'preview_show',
          args: { url: 'http://localhost:4173/' }, content: { url: 'http://localhost:4173/' }, status: 'called', timestamp: 2,
        },
      },
      {
        event: 'message',
        data: {
          event_id: 'user-2', turn_id: 'turn-2', role: 'user', content: 'Research something else', attachments: [], timestamp: 3,
        },
      },
      {
        event: 'tool',
        data: {
          event_id: 'browser-2', turn_id: 'turn-2', tool_call_id: 'browser-2', name: 'browser', function: 'browser_navigate',
          args: { url: 'https://example.com/' }, content: { screenshot: '/signed/shot' }, status: 'called', timestamp: 4,
        },
      },
    ] as AgentEvent[]));
    const wrapper = await mountPage();

    await wrapper.get('[data-testid="workspace-button"]').trigger('click');

    expect(computerPanelShow).toHaveBeenCalledWith(
      expect.objectContaining({ tool_call_id: 'browser-2', name: 'browser' }),
      false,
    );
    wrapper.unmount();
  });

  it('blocks a second submission while the optimistic turn is running', async () => {
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);

    chatBox.vm.$emit('update:modelValue', 'first task');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    expect(submittedCalls()).toHaveLength(1);
    expect(chatBox.props('isRunning')).toBe(true);

    chatBox.vm.$emit('update:modelValue', 'must stay a draft');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    expect(submittedCalls()).toHaveLength(1);
    wrapper.unmount();
  });

  it('lets status_update, not terminal timeline events or stream_end, finish a turn', async () => {
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'finish once');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();
    const callbacks = currentChatCallbacks();

    callbacks?.onMessage?.({
      event: 'done',
      data: { event_id: 'done-1', timestamp: 1 },
    });
    callbacks?.onClose?.();
    await nextTick();
    expect(chatBox.props('isRunning')).toBe(true);

    callbacks?.onStatusUpdate?.('completed');
    await nextTick();
    expect(chatBox.props('isRunning')).toBe(false);
    wrapper.unmount();
  });

  it('keeps files in a files-only initial WebSocket submission with a durable UUID', async () => {
    const file = {
      file_id: 'file-only-1',
      filename: 'report.pdf',
      size: 10,
      upload_date: '2026-01-01T00:00:00Z',
    };
    window.history.replaceState({ files: [file] }, '', '/chat/session-1');

    const wrapper = await mountPage();

    expect(agentMocks.getSession).toHaveBeenCalledOnce();
    expect(agentMocks.chatWithSession).toHaveBeenCalledOnce();
    const call = agentMocks.chatWithSession.mock.calls[0]!;
    expect(call[1]).toBe('');
    expect(call[3]).toEqual([
      { file_id: 'file-only-1', filename: 'report.pdf' },
    ]);
    expect(call).toHaveLength(6);
    expect(call[5]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(wrapper.getComponent(ChatBoxStub).props('isRunning')).toBe(true);
    wrapper.unmount();
  });

  it('resumes a running session and unlocks when status changes to waiting', async () => {
    agentMocks.getSession.mockResolvedValue(runningSession([{
      event: 'message',
      data: {
        event_id: 'user-1',
        role: 'user',
        content: 'run',
        attachments: [],
        timestamp: 1,
      },
    }]));

    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    expect(chatBox.props('isRunning')).toBe(true);
    expect(agentMocks.chatWithSession).toHaveBeenCalledWith(
      'session-1',
      '',
      'user-1',
      undefined,
      expect.any(Object),
    );

    currentChatCallbacks()?.onStatusUpdate?.('waiting');
    await nextTick();
    expect(chatBox.props('isRunning')).toBe(false);
    wrapper.unmount();
  });

  it('deduplicates stop requests and clears the optimistic busy phase on success', async () => {
    let resolveStop!: () => void;
    agentMocks.stopSession.mockReturnValue(new Promise<void>((resolve) => {
      resolveStop = resolve;
    }));
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'long task');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    chatBox.vm.$emit('stop');
    await nextTick();
    chatBox.vm.$emit('stop');
    expect(agentMocks.stopSession).toHaveBeenCalledOnce();
    expect(chatBox.props('isStopping')).toBe(true);

    resolveStop();
    await flushPromises();
    expect(chatBox.props('isStopping')).toBe(false);
    expect(chatBox.props('isRunning')).toBe(false);
    wrapper.unmount();
  });

  it('keeps a possibly accepted turn busy after a WebSocket error', async () => {
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'network task');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    currentChatCallbacks()?.onError?.(new Error('socket failed'));
    await flushPromises();
    expect(chatBox.props('isRunning')).toBe(true);

    chatBox.vm.$emit('update:modelValue', 'retry safely');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();
    expect(submittedCalls()).toHaveLength(1);
    wrapper.unmount();
  });

  it('does not unlock when submission delivery remains uncertain after retries', async () => {
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    agentMocks.chatWithSession.mockRejectedValueOnce(new ChatSubmissionError(
      'Chat WS chat timed out',
      'fce9cb69-0490-4bf8-a107-cb03f1c59983',
      true,
    ));

    chatBox.vm.$emit('update:modelValue', 'uncertain task');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    expect(chatBox.props('isRunning')).toBe(true);
    expect(submittedCalls()).toHaveLength(1);
    wrapper.unmount();
  });

  it('passes one stable UUID for a submitted durable turn', async () => {
    const wrapper = await mountPage();
    const chatBox = wrapper.getComponent(ChatBoxStub);
    chatBox.vm.$emit('update:modelValue', 'stable turn');
    await nextTick();
    chatBox.vm.$emit('submit');
    await flushPromises();

    const call = submittedCalls()[0]!;
    expect(call[5]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    wrapper.unmount();
  });

  it('clears local handlers and leaves the channel on unmount', async () => {
    const cancel = vi.fn();
    agentMocks.chatWithSession.mockResolvedValue(cancel);
    const wrapper = await mountPage();

    wrapper.unmount();

    expect(cancel).toHaveBeenCalledOnce();
    expect(agentMocks.leaveChatSession).toHaveBeenCalledWith('session-1');
  });
});
