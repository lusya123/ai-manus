<template>
  <ToolViewHeader :title="shellSessionId" />
  <div class="flex-1 min-h-0 w-full overflow-y-auto">
    <div dir="ltr" data-orientation="horizontal" class="flex flex-col flex-1 min-h-0">
      <div
        class="py-2 flex-1 font-mono text-sm leading-relaxed px-3 outline-none overflow-auto whitespace-pre-wrap break-all">
        <div data-testid="shell-console">
          <div v-for="(record, index) in shellRecords" :key="index">
            <div>
              <span class="text-[#00bb00]">{{ record.ps1 }}</span><span> {{ record.command }}</span>
            </div>
            <div>{{ record.output }}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, toRef } from 'vue';
import { viewShellSession } from '@/api/agent';
import { ToolContent } from '@/types/message';
import type { ConsoleRecord } from '@/types/response';
import ToolViewHeader from './ToolViewHeader.vue';
import { useLiveToolContent } from '@/composables/useLiveToolContent';

const props = defineProps<{
  sessionId: string;
  toolContent: ToolContent;
  live: boolean;
}>();

defineExpose({
  loadContent: () => {
    loadShellContent();
  }
});

const shellRecords = ref<ConsoleRecord[]>([]);

// Get shellSessionId from toolContent
const shellSessionId = computed(() => {
  if (props.toolContent && props.toolContent.args.id) {
    return props.toolContent.args.id;
  }
  return '';
});

const updateShellContent = (records: ConsoleRecord[] | null | undefined) => {
  shellRecords.value = Array.isArray(records) ? records : [];
};

// Function to load Shell session content
const loadShellContent = async () => {
  if (!props.live) {
    updateShellContent(props.toolContent.content?.console);
    return;
  }

  if (!shellSessionId.value) return;

  try {
    const response = await viewShellSession(props.sessionId, shellSessionId.value);
    updateShellContent(response.console);
  } catch (error) {
    console.error("Failed to load shell content:", error);
  }
};

useLiveToolContent({
  toolContent: toRef(props, 'toolContent'),
  live: toRef(props, 'live'),
  targetKey: shellSessionId,
  load: loadShellContent,
});
</script>
