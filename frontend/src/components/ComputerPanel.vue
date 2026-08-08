<template>
  <!-- Official ManusComputer presentation=sidebar | dialog -->
  <div
    ref="computerPanelRef"
    v-if="visible && presentation === 'sidebar'"
    :class="{
      'h-full w-full top-0 ltr:right-0 rtl:left-0 z-50 fixed sm:sticky sm:top-0 sm:right-0 sm:h-[100vh] sm:min-w-[520px]': isShow,
      'h-full overflow-hidden': !isShow
    }"
    :style="{ width: isShow ? `${sideWidth}px` : '0px', opacity: isShow ? '1' : '0', transition: '0.2s ease-in-out' }">
    <div class="h-full" :style="{ width: isShow ? '100%' : '0px' }">
      <ComputerPanelContent
        v-if="isShow && toolContent"
        presentation="sidebar"
        :sessionId="sessionId"
        :realTime="realTime"
        :toolContent="toolContent"
        :live="live"
        :isShare="isShare"
        :toolHistory="toolHistory"
        :plan="plan"
        @hide="hideComputerPanel"
        @jumpToRealTime="jumpToRealTime"
        @selectTool="onSelectTool"
        @useComputer="onUseComputer"
        @toggle-presentation="togglePresentation"
      />
    </div>
  </div>

  <button v-if="visible && !isShow && toolContent" @click="reopenComputerPanel"
    data-testid="reopen-workspace-button"
    class="fixed right-4 bottom-24 z-40 h-10 px-3 rounded-full inline-flex items-center gap-2 bg-[var(--background-white-main)] text-[var(--text-primary)] border border-[var(--border-main)] shadow-[0px_5px_16px_0px_var(--shadow-S),0px_0px_1.25px_0px_var(--shadow-S)] hover:bg-[var(--background-gray-main)] cursor-pointer"
    :title="$t('Open Manus workspace')">
    <Monitor class="size-4 text-[var(--icon-secondary)]" />
    <span class="text-sm font-medium whitespace-nowrap">{{ $t('Open workspace') }}</span>
  </button>

  <Teleport to="body">
    <!-- Official ChatComputerDialogPanel (z-upper → z-[1100]) -->
    <div
      v-if="visible && isShow && toolContent && presentation === 'dialog'"
      class="fixed inset-0 w-full z-[1100] flex items-center justify-center py-[12px] bg-[var(--background-mask-black)] backdrop-blur-[12px]"
      @click.self="hideComputerPanel">
      <div
        class="!w-[900px] max-w-[95%] max-h-[1200px] h-full z-10"
        @click.stop>
        <ComputerPanelContent
          presentation="dialog"
          :sessionId="sessionId"
          :realTime="realTime"
          :toolContent="toolContent"
          :live="live"
          :isShare="isShare"
          :toolHistory="toolHistory"
          :plan="plan"
          @hide="hideComputerPanel"
          @jumpToRealTime="jumpToRealTime"
          @selectTool="onSelectTool"
          @useComputer="onUseComputer"
          @toggle-presentation="togglePresentation"
        />
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { Monitor } from 'lucide-vue-next'
import type { ToolContent } from '../types/message'
import type { PlanEventData } from '../types/event'
import ComputerPanelContent from './ComputerPanelContent.vue'
import { useResizeObserver } from '../composables/useResizeObserver'
import { eventBus, UI_SHOW_FILE_PREVIEWER, UI_SHOW_COMPUTER_PANEL } from '../utils/eventBus'

export type ComputerPresentation = 'sidebar' | 'dialog'

const computerPanelRef = ref<HTMLElement>()
const { size: parentSize } = useResizeObserver(computerPanelRef, {
  target: 'parent',
  property: 'width'
})

const isShow = ref(false)
const live = ref(false)
const toolContent = ref<ToolContent>()
const visible = ref(true)
const presentation = ref<ComputerPresentation>('sidebar')

const sideWidth = computed(() => {
  const p = parentSize.value || 520
  return Math.min(Math.max(p / 2, Math.min(520, p)), p)
})

const emit = defineEmits<{
  (e: 'jumpToRealTime'): void
  (e: 'selectTool', tool: ToolContent): void
  (e: 'useComputer'): void
}>()

const props = defineProps<{
  sessionId?: string
  realTime: boolean
  isShare: boolean
  toolHistory?: ToolContent[]
  plan?: PlanEventData | null
}>()

const showComputerPanel = (content: ToolContent, isLive: boolean = false) => {
  // Notify file previewer: dismiss side mode only (center/fullscreen stay)
  eventBus.emit(UI_SHOW_COMPUTER_PANEL)
  visible.value = true
  toolContent.value = content
  isShow.value = true
  live.value = isLive
}

const hideComputerPanel = () => {
  isShow.value = false
  presentation.value = 'sidebar'
}

const clearComputerPanel = () => {
  isShow.value = false
  visible.value = true
  live.value = false
  toolContent.value = undefined
  presentation.value = 'sidebar'
}

const reopenComputerPanel = () => {
  if (toolContent.value) showComputerPanel(toolContent.value, live.value)
}

const togglePresentation = () => {
  presentation.value = presentation.value === 'sidebar' ? 'dialog' : 'sidebar'
}

const jumpToRealTime = () => {
  emit('jumpToRealTime')
}

const onSelectTool = (tool: ToolContent) => {
  toolContent.value = tool
  live.value = false
  emit('selectTool', tool)
}

const onUseComputer = () => {
  emit('useComputer')
}

/** Opening file previewer hides the computer panel (mutual exclusion). */
const onShowFilePreviewer = () => {
  visible.value = false
}

onMounted(() => {
  eventBus.on(UI_SHOW_FILE_PREVIEWER, onShowFilePreviewer)
})

onUnmounted(() => {
  eventBus.off(UI_SHOW_FILE_PREVIEWER, onShowFilePreviewer)
})

watch(() => props.sessionId, clearComputerPanel)

defineExpose({
  showComputerPanel,
  hideComputerPanel,
  clearComputerPanel,
  isShow
})
</script>
