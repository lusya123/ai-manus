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
      v-if="previewSrc && canOpenExternally"
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
      sandbox="allow-downloads allow-forms allow-scripts"
      referrerpolicy="no-referrer"
    />
    <div v-else-if="previewError" class="h-full flex flex-col gap-3 items-center justify-center text-sm text-[var(--text-tertiary)]">
      <span>{{ t('Preview unavailable') }}</span>
      <button
        type="button"
        class="h-8 px-3 rounded-lg border border-[var(--border-btn-main)] text-[var(--text-primary)] hover:bg-[var(--fill-tsp-white-light)]"
        @click="loadPreview"
      >
        {{ t('Retry') }}
      </button>
    </div>
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
const previewError = ref(false);
const isProxiedPreview = ref(false);
let previewRequestId = 0;

const rawUrl = computed(() => {
  return props.toolContent?.content?.url || props.toolContent?.args?.url || '';
});

const previewTitle = computed(() => {
  return props.toolContent?.content?.title || props.toolContent?.args?.title || rawUrl.value || 'Preview';
});

const canOpenExternally = computed(() => {
  if (!previewSrc.value || isProxiedPreview.value) return false;
  try {
    return new URL(previewSrc.value, window.location.origin).origin !== window.location.origin;
  } catch {
    return false;
  }
});

const withApiHost = (url: string) => {
  if (/^https?:\/\//i.test(url)) {
    return url;
  }
  return `${API_CONFIG.host || ''}${url}`;
};

const loadPreview = async () => {
  const requestId = ++previewRequestId;
  previewSrc.value = '';
  previewError.value = false;
  isProxiedPreview.value = false;
  if (!props.sessionId || !rawUrl.value) {
    return;
  }

  try {
    const signed = await createPreviewUrl(props.sessionId, rawUrl.value, {
      publicAccess: props.isShare,
    });
    if (requestId === previewRequestId) {
      const resolvedUrl = withApiHost(signed.signed_url);
      previewSrc.value = resolvedUrl;
      try {
        isProxiedPreview.value = /\/api\/v1\/sessions\/[^/]+\/preview\//.test(
          new URL(resolvedUrl, window.location.origin).pathname,
        );
      } catch {
        isProxiedPreview.value = true;
      }
    }
  } catch (error) {
    console.error('Failed to create preview URL:', error);
    if (requestId === previewRequestId) {
      previewError.value = true;
    }
  }
};

watch(
  () => [props.sessionId, rawUrl.value, props.toolContent.timestamp],
  () => loadPreview(),
  { immediate: true }
);
</script>
