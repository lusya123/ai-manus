<template>
  <div
    ref="toolPanelRef"
    v-if="visible"
    :class="{
      'h-full w-full top-0 ltr:right-0 rtl:left-0 z-50 fixed sm:sticky sm:top-0 sm:right-0 sm:h-[100vh] sm:ml-3 sm:py-3 sm:mr-4': isShow,
      'h-full overflow-hidden': !isShow 
    }"
    :style="{ 'width': isShow ? `${parentSize/2}px` : '0px', 'opacity': isShow ? '1' : '0', 'transition': '0.2s ease-in-out' }">
    <div class="h-full" :style="{ 'width': isShow ? '100%' : '0px' }">
      <ToolPanelContent v-if="isShow && toolContent" :sessionId="sessionId" :realTime="realTime" :toolContent="toolContent" :live="live" :isShare="isShare" @hide="hideToolPanel" @jumpToRealTime="jumpToRealTime" />
    </div>
  </div>
  <button
    v-if="visible && !isShow && toolContent"
    @click="reopenToolPanel"
    class="fixed right-4 bottom-24 z-40 h-10 px-3 rounded-full inline-flex items-center gap-2 bg-[var(--background-white-main)] text-[var(--text-primary)] border border-[var(--border-main)] shadow-[0px_5px_16px_0px_var(--shadow-S),0px_0px_1.25px_0px_var(--shadow-S)] hover:bg-[var(--background-gray-main)] cursor-pointer"
    :title="$t('Open Manus workspace')">
    <Monitor class="size-4 text-[var(--icon-secondary)]" />
    <span class="text-sm font-medium whitespace-nowrap">{{ $t('Open workspace') }}</span>
  </button>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { Monitor } from 'lucide-vue-next'
import type { ToolContent } from '../types/message'
import ToolPanelContent from './ToolPanelContent.vue'
import { useResizeObserver } from '../composables/useResizeObserver'
import { eventBus } from '../utils/eventBus'
import { EVENT_SHOW_FILE_PANEL, EVENT_SHOW_TOOL_PANEL } from '../constants/event'

const toolPanelRef = ref<HTMLElement>()
const { size: parentSize } = useResizeObserver(toolPanelRef, {
  target: 'parent',
  property: 'width'
})

// Tool panel state
const isShow = ref(false)
const live = ref(false)
const toolContent = ref<ToolContent>()
const visible = ref(true)

const emit = defineEmits<{
  (e: 'jumpToRealTime'): void
}>()

const props = defineProps<{
  size?: number
  sessionId?: string
  realTime: boolean
  isShare: boolean
}>()

const showToolPanel = (content: ToolContent, isLive: boolean = false) => {
  eventBus.emit(EVENT_SHOW_TOOL_PANEL)
  visible.value = true
  toolContent.value = content
  isShow.value = true
  live.value = isLive
}

const hideToolPanel = () => {
  isShow.value = false
}

const clearToolPanel = () => {
  isShow.value = false
  visible.value = true
  live.value = false
  toolContent.value = undefined
}

const reopenToolPanel = () => {
  if (toolContent.value) {
    showToolPanel(toolContent.value, live.value)
  }
}

const jumpToRealTime = () => {
  emit('jumpToRealTime')
}

const handleFilePanelShown = () => {
  visible.value = false
}

onMounted(() => {
  eventBus.on(EVENT_SHOW_FILE_PANEL, handleFilePanelShown)
})

onUnmounted(() => {
  eventBus.off(EVENT_SHOW_FILE_PANEL, handleFilePanelShown)
})

watch(() => props.sessionId, () => {
  clearToolPanel()
})

defineExpose({
  showToolPanel,
  hideToolPanel,
  clearToolPanel,
  isShow
})
</script>
