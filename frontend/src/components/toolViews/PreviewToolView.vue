<template>
  <div
    class="h-[36px] flex items-center px-3 w-full bg-[var(--background-gray-main)] border-b border-[var(--border-main)] rounded-t-[12px] shadow-[inset_0px_1px_0px_0px_#FFFFFF] dark:shadow-[inset_0px_1px_0px_0px_#FFFFFF30]"
  >
    <div class="flex-1 flex items-center justify-center min-w-0">
      <div class="max-w-[420px] truncate text-[var(--text-tertiary)] text-sm font-medium text-center">
        {{ previewTitle }}
      </div>
    </div>
    <a
      v-if="previewSrc"
      :href="previewSrc"
      target="_blank"
      rel="noopener noreferrer"
      class="w-7 h-7 inline-flex items-center justify-center rounded-md hover:bg-[var(--fill-tsp-gray-main)]"
      :title="t('Open preview')"
    >
      <ExternalLink class="w-4 h-4 text-[var(--icon-tertiary)]" />
    </a>
  </div>
  <div class="flex-1 min-h-0 w-full overflow-hidden bg-[var(--fill-white)]">
    <iframe
      v-if="previewSrc"
      :key="previewSrc"
      :src="previewSrc"
      class="w-full h-full border-0 bg-white"
      sandbox="allow-downloads allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-same-origin allow-scripts"
      referrerpolicy="no-referrer"
    />
    <div v-else class="h-full flex items-center justify-center text-sm text-[var(--text-tertiary)]">
      {{ t('Preparing preview') }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import { ExternalLink } from 'lucide-vue-next';
import type { ToolContent } from '@/types/message';
import { API_CONFIG } from '@/api/client';
import { createPreviewUrl } from '@/api/agent';

const props = defineProps<{
  sessionId?: string;
  toolContent: ToolContent;
  live: boolean;
  isShare: boolean;
}>();

const { t } = useI18n();
const previewSrc = ref('');

const rawUrl = computed(() => {
  return props.toolContent?.content?.url || props.toolContent?.args?.url || '';
});

const previewTitle = computed(() => {
  return props.toolContent?.content?.title || props.toolContent?.args?.title || rawUrl.value || 'Preview';
});

const withApiHost = (url: string) => {
  if (/^https?:\/\//i.test(url)) {
    return url;
  }
  return `${API_CONFIG.host || ''}${url}`;
};

const loadPreview = async () => {
  previewSrc.value = '';
  if (!props.sessionId || !rawUrl.value) {
    return;
  }

  try {
    const signed = await createPreviewUrl(props.sessionId, rawUrl.value);
    previewSrc.value = withApiHost(signed.signed_url);
  } catch (error) {
    console.error('Failed to create preview URL:', error);
  }
};

watch(
  () => [props.sessionId, rawUrl.value, props.toolContent.timestamp],
  () => loadPreview(),
  { immediate: true }
);
</script>
