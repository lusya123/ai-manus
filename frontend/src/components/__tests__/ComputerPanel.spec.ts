import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import { defineComponent, nextTick } from 'vue';
import { mount } from '@vue/test-utils';
import ComputerPanel from '../ComputerPanel.vue';
import { i18n } from '../../composables/useI18n';
import type { ToolContent } from '../../types/message';

const ComputerPanelContentStub = defineComponent({
  name: 'ComputerPanelContent',
  props: ['toolContent'],
  emits: ['hide'],
  template: '<button data-testid="close-workspace" @click="$emit(\'hide\')">close</button>',
});

const previewTool: ToolContent = {
  timestamp: 1,
  tool_call_id: 'preview-1',
  name: 'preview',
  function: 'preview_show',
  args: { url: 'http://localhost:4173/' },
  content: { url: 'http://localhost:4173/' },
  status: 'called',
};

type ComputerPanelExposed = {
  showComputerPanel: (tool: ToolContent, live?: boolean) => void;
};

beforeAll(() => {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
});

describe('ComputerPanel reopening', () => {
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('reopens the same workspace after the panel is closed', async () => {
    const wrapper = mount(ComputerPanel, {
      props: {
        sessionId: 'session-1',
        realTime: false,
        isShare: false,
      },
      global: {
        plugins: [i18n],
        stubs: { ComputerPanelContent: ComputerPanelContentStub },
      },
      attachTo: document.body,
    });

    (wrapper.vm as unknown as ComputerPanelExposed).showComputerPanel(previewTool, false);
    await nextTick();
    expect(wrapper.getComponent(ComputerPanelContentStub).props('toolContent')).toEqual(previewTool);

    await wrapper.get('[data-testid="close-workspace"]').trigger('click');
    expect(wrapper.find('[data-testid="reopen-workspace-button"]').exists()).toBe(true);

    await wrapper.get('[data-testid="reopen-workspace-button"]').trigger('click');
    expect(wrapper.getComponent(ComputerPanelContentStub).props('toolContent')).toEqual(previewTool);
    wrapper.unmount();
  });

  it('clears the closed workspace when the route changes to another session', async () => {
    const wrapper = mount(ComputerPanel, {
      props: {
        sessionId: 'session-1',
        realTime: false,
        isShare: false,
      },
      global: {
        plugins: [i18n],
        stubs: { ComputerPanelContent: ComputerPanelContentStub },
      },
    });

    (wrapper.vm as unknown as ComputerPanelExposed).showComputerPanel(previewTool, false);
    await nextTick();
    await wrapper.get('[data-testid="close-workspace"]').trigger('click');
    await wrapper.setProps({ sessionId: 'session-2' });

    expect(wrapper.find('[data-testid="reopen-workspace-button"]').exists()).toBe(false);
    wrapper.unmount();
  });
});
