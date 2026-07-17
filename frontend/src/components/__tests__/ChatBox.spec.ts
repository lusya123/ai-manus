import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import ChatBox from '../ChatBox.vue';
import ChatBoxFiles from '../ChatBoxFiles.vue';
import { i18n } from '../../composables/useI18n';
import type { FileInfo } from '../../api/file';

const mountChatBox = (modelValue = 'hello') => mount(ChatBox, {
  props: {
    modelValue,
    rows: 1,
    isRunning: false,
    attachments: [],
  },
  global: { plugins: [i18n] },
});

describe('ChatBox submission', () => {
  it('submits on Enter using the textarea value', async () => {
    const wrapper = mountChatBox('');
    const textarea = wrapper.get('textarea');
    await textarea.setValue('fresh draft');
    await textarea.trigger('keydown', { key: 'Enter' });

    const modelUpdates = wrapper.emitted('update:modelValue') || [];
    expect(modelUpdates[modelUpdates.length - 1]).toEqual(['fresh draft']);
    expect(wrapper.emitted('submit')).toHaveLength(1);
  });

  it('does not submit Enter while an IME composition is active', async () => {
    const wrapper = mountChatBox('中文');
    const textarea = wrapper.get('textarea');
    await textarea.trigger('compositionstart');
    await textarea.trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('submit')).toBeUndefined();

    await textarea.trigger('compositionend');
    await textarea.trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('submit')).toHaveLength(1);
  });

  it('does not pass the click MouseEvent into draft validation', async () => {
    const wrapper = mountChatBox();
    const buttons = wrapper.findAll('button');
    await buttons[buttons.length - 1].trigger('click');
    expect(wrapper.emitted('submit')).toHaveLength(1);
  });

  it('forwards attachment updates from ChatBoxFiles', async () => {
    const wrapper = mountChatBox();
    const files: FileInfo[] = [{
      file_id: 'file-1',
      filename: 'one.txt',
      size: 1,
      upload_date: '2026-01-01T00:00:00Z',
    }];
    wrapper.getComponent(ChatBoxFiles).vm.$emit('update:attachments', files);
    await wrapper.vm.$nextTick();
    expect(wrapper.emitted('update:attachments')).toEqual([[files]]);
  });

  it('does not send text while an attached file is still uploading', async () => {
    const uploadingFile = {
      file_id: 'temp-1',
      filename: 'pending.txt',
      size: 1,
      upload_date: '2026-01-01T00:00:00Z',
      status: 'uploading',
    } as FileInfo & { status: 'uploading' | 'success' };
    const wrapper = mount(ChatBox, {
      props: {
        modelValue: 'send with attachment',
        rows: 1,
        isRunning: false,
        allowSendFilesOnly: true,
        attachments: [uploadingFile],
      },
      global: { plugins: [i18n] },
    });
    const footerButtons = wrapper.findAll('footer button');
    const sendButton = footerButtons[footerButtons.length - 1];

    await sendButton.trigger('click');
    expect(wrapper.emitted('submit')).toBeUndefined();

    const uploadedFile = { ...uploadingFile, status: 'success' } as typeof uploadingFile;
    await wrapper.setProps({ attachments: [uploadedFile] });
    await sendButton.trigger('click');
    expect(wrapper.emitted('submit')).toHaveLength(1);
  });
});
