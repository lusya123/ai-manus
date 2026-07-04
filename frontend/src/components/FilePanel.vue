<template>
  <div
    ref="filePanelRef"
    v-if="visible"
    :class="{
      'h-full w-full top-0 ltr:right-0 rtl:left-0 z-50 fixed sm:sticky sm:top-0 sm:h-[100vh]': isShow,
      'h-full overflow-hidden': !isShow 
    }"
    :style="{ 'width': isShow ? `${parentSize/2}px` : '0px', 'opacity': isShow ? '1' : '0', 'transition': '0.2s ease-in-out' }">
    <div class="h-full" :style="{ 'width': isShow ? '100%' : '0px' }">
      <div v-if="isShow && fileInfo && fileType" class="bg-[var(--background-gray-main)] overflow-hidden shadow-[0px_0px_8px_0px_rgba(0,0,0,0.02)] ltr:border-l rtl:border-r border-black/8 dark:border-[var(--border-light)] flex flex-col h-full w-full">
        <div
          class="px-4 pt-2 pb-4 gap-4 flex items-center justify-between flex-shrink-0 border-b border-[var(--border-main)] flex-col-reverse md:flex-row md:py-4">
          <div class="flex justify-between self-stretch flex-1 truncate">
            <div
              class="flex flex-row gap-1 items-center text-[var(--text-secondary)] font-medium truncate [&amp;_svg]:flex-shrink-0">
              <a href="" class="p-1 flex-shrink-0 cursor-default" target="_blank">
                <div class="relative flex items-center justify-center">
                  <component :is="fileType.icon" />
                </div>
              </a>
              <div class="truncate flex flex-col"><span class="truncate" :title="fileInfo.filename">{{ fileInfo.filename }}</span></div>
            </div>
          </div>
          <div class="flex items-center justify-between gap-2 w-full py-3 md:w-auto md:py-0 select-none">
            <div class="flex items-center gap-2">
              <div @click="download"
                class="flex h-7 w-7 items-center justify-center cursor-pointer hover:bg-[var(--fill-tsp-gray-main)] rounded-md"
                aria-expanded="false" aria-haspopup="dialog">
                <Download class="text-[var(--icon-secondary)] size-[18px]" />
              </div>
            </div>
            <div class="flex items-center gap-2">
              <div @click="hideFilePanel"
                class="flex h-7 w-7 items-center justify-center cursor-pointer hover:bg-[var(--fill-tsp-gray-main)] rounded-md">
                <X class="size-5 text-[var(--icon-secondary)]" />
              </div>
            </div>
          </div>
        </div>
        <component :is="fileType.preview" :file="fileInfo" />
      </div>
    </div>
  </div>
  <button
    v-if="visible && !isShow && fileInfo"
    @click="showFilePanel(fileInfo)"
    class="fixed right-4 bottom-24 z-40 h-10 px-3 rounded-full inline-flex items-center gap-2 bg-[var(--background-white-main)] text-[var(--text-primary)] border border-[var(--border-main)] shadow-[0px_5px_16px_0px_var(--shadow-S),0px_0px_1.25px_0px_var(--shadow-S)] hover:bg-[var(--background-gray-main)] cursor-pointer"
    :title="$t('Open file')">
    <FileText class="size-4 text-[var(--icon-secondary)]" />
    <span class="text-sm font-medium max-w-[160px] truncate">{{ $t('Open file') }}</span>
  </button>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Download, FileText, X } from 'lucide-vue-next'
import { useFilePanel } from '../composables/useFilePanel'
import { getFileDownloadUrl } from '../api/file'
import { getFileType } from '../utils/fileType'
import { useResizeObserver } from '../composables/useResizeObserver'
import { eventBus } from '../utils/eventBus'
import { EVENT_SHOW_TOOL_PANEL } from '../constants/event'


const {
  isShow,
  fileInfo,
  visible,
  showFilePanel,
  hideFilePanel
} = useFilePanel()

const route = useRoute()

const filePanelRef = ref<HTMLElement>()
const { size: parentSize } = useResizeObserver(filePanelRef, {
  target: 'parent',
  property: 'width'
})

const fileType = computed(() => {
  if (!fileInfo.value) return null
  return getFileType(fileInfo.value.filename)
})

const download = async () => {
  if (!fileInfo.value) return
  const url = await getFileDownloadUrl(fileInfo.value)
  window.open(url, '_blank')
}

const handleToolPanelShown = () => {
  visible.value = false
}

const clearFilePanel = () => {
  isShow.value = false
  visible.value = true
  fileInfo.value = undefined
}

onMounted(() => {
  eventBus.on(EVENT_SHOW_TOOL_PANEL, handleToolPanelShown)
})

onUnmounted(() => {
  eventBus.off(EVENT_SHOW_TOOL_PANEL, handleToolPanelShown)
})

watch(() => route.fullPath, () => {
  clearFilePanel()
})

defineExpose({
  showFilePanel,
  hideFilePanel,
  clearFilePanel,
  isShow
})
</script>
