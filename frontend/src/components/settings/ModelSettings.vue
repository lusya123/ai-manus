<template>
  <div class="pb-[32px] last:pb-0 border-b border-[var(--border-light)] last-of-type:border-transparent w-full">
    <div class="text-[13px] font-medium text-[var(--text-tertiary)] mb-1 w-full">{{ t('Model') }}</div>
    <div class="flex flex-col gap-5 w-full max-w-[520px]">
      <div class="flex flex-col gap-2">
        <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Model mode') }}</div>
        <Select v-model="mode">
          <SelectTrigger class="w-full h-[36px]">
            <SelectValue :placeholder="t('Select model mode')" />
          </SelectTrigger>
          <SelectContent :side-offset="5">
            <SelectItem value="system">{{ t('System default') }}</SelectItem>
            <SelectItem value="custom">{{ t('Custom model') }}</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <template v-if="mode === 'custom'">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div class="flex flex-col gap-2">
            <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Model') }}</div>
            <Select v-model="selectedModel" @update:modelValue="handleModelSelect">
              <SelectTrigger class="w-full h-[36px]">
                <SelectValue :placeholder="t('Select model')" />
              </SelectTrigger>
              <SelectContent :side-offset="5">
                <SelectItem
                  v-for="option in modelOptions"
                  :key="option.value"
                  :value="option.value"
                >
                  {{ option.label }}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div class="flex flex-col gap-2">
            <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Provider') }}</div>
            <Select v-model="form.model_provider">
              <SelectTrigger class="w-full h-[36px]">
                <SelectValue :placeholder="t('Select provider')" />
              </SelectTrigger>
              <SelectContent :side-offset="5">
                <SelectItem value="openai">{{ t('OpenAI compatible') }}</SelectItem>
                <SelectItem value="anthropic">{{ t('Anthropic') }}</SelectItem>
                <SelectItem value="google_genai">{{ t('Google') }}</SelectItem>
                <SelectItem value="custom">{{ t('Custom provider') }}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <div v-if="selectedModel === CUSTOM_MODEL_VALUE" class="flex flex-col gap-2">
          <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Model name') }}</div>
          <div class="rounded-[10px] overflow-hidden text-sm leading-[22px] text-[var(--text-primary)] h-10 flex items-center bg-[var(--fill-tsp-white-main)] pt-2 pr-3 pb-2 pl-4 focus-within:ring-[1.5px] focus-within:ring-[var(--border-dark)] w-full">
            <input
              v-model="form.model_name"
              class="h-full min-w-1 flex-1 bg-transparent disabled:cursor-not-allowed placeholder:text-[var(--text-disable)]"
              :placeholder="t('Enter model name')"
            />
          </div>
        </div>

        <div v-if="form.model_provider === 'custom'" class="flex flex-col gap-2">
          <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('Provider') }}</div>
          <div class="rounded-[10px] overflow-hidden text-sm leading-[22px] text-[var(--text-primary)] h-10 flex items-center bg-[var(--fill-tsp-white-main)] pt-2 pr-3 pb-2 pl-4 focus-within:ring-[1.5px] focus-within:ring-[var(--border-dark)] w-full">
            <input
              v-model="customProvider"
              class="h-full min-w-1 flex-1 bg-transparent disabled:cursor-not-allowed placeholder:text-[var(--text-disable)]"
              :placeholder="t('Enter provider')"
            />
          </div>
        </div>

        <div class="flex flex-col gap-2">
          <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('API Base URL') }}</div>
          <div class="rounded-[10px] overflow-hidden text-sm leading-[22px] text-[var(--text-primary)] h-10 flex items-center bg-[var(--fill-tsp-white-main)] pt-2 pr-3 pb-2 pl-4 focus-within:ring-[1.5px] focus-within:ring-[var(--border-dark)] w-full">
            <input
              v-model="form.api_base"
              class="h-full min-w-1 flex-1 bg-transparent disabled:cursor-not-allowed placeholder:text-[var(--text-disable)]"
              placeholder="https://api.openai.com/v1"
            />
          </div>
        </div>

        <div class="flex flex-col gap-2">
          <div class="text-sm font-medium text-[var(--text-primary)]">{{ t('API Key') }}</div>
          <div class="rounded-[10px] overflow-hidden text-sm leading-[22px] text-[var(--text-primary)] h-10 flex items-center gap-2 bg-[var(--fill-tsp-white-main)] pt-2 pr-3 pb-2 pl-4 focus-within:ring-[1.5px] focus-within:ring-[var(--border-dark)] w-full">
            <input
              v-model="form.api_key"
              class="h-full min-w-1 flex-1 bg-transparent disabled:cursor-not-allowed placeholder:text-[var(--text-disable)]"
              :placeholder="t('Enter API key')"
              :type="showApiKey ? 'text' : 'password'"
            />
            <button
              type="button"
              class="flex items-center justify-center size-6 rounded-md hover:bg-[var(--fill-tsp-white-light)]"
              :title="showApiKey ? t('Hide API key') : t('Show API key')"
              @click="showApiKey = !showApiKey"
            >
              <EyeOff v-if="showApiKey" class="size-4 text-[var(--icon-secondary)]" />
              <Eye v-else class="size-4 text-[var(--icon-secondary)]" />
            </button>
          </div>
        </div>
      </template>

      <div class="flex items-center justify-between gap-3 max-sm:flex-col max-sm:items-stretch">
        <div class="text-xs text-[var(--text-tertiary)] min-h-[18px]">
          {{ statusText }}
        </div>
        <div class="flex gap-2 max-sm:justify-end">
          <button
            type="button"
            class="inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 outline outline-1 -outline-offset-1 hover:bg-[var(--fill-tsp-white-light)] text-[var(--text-primary)] outline-[var(--border-btn-main)] bg-transparent h-[32px]"
            @click="resetToSystem"
          >
            <RotateCcw class="size-4" />
            {{ t('Reset') }}
          </button>
          <button
            type="button"
            class="inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors hover:opacity-90 active:opacity-80 px-[12px] rounded-[10px] gap-[6px] text-sm min-w-16 text-white bg-[var(--Button-primray-black)] h-[32px]"
            @click="saveConfig"
          >
            <Save class="size-4" />
            {{ t('Save') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Eye, EyeOff, RotateCcw, Save } from 'lucide-vue-next'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  clearStoredAgentConfig,
  getStoredAgentConfig,
  saveStoredAgentConfig,
} from '@/api/agentConfig'
import type { StoredAgentConfig } from '@/api/agentConfig'
import { showErrorToast, showSuccessToast } from '@/utils/toast'

const CUSTOM_MODEL_VALUE = '__custom__'

type ModelMode = 'system' | 'custom'

interface ModelOption {
  value: string
  label: string
  provider?: string
}

const { t } = useI18n()
const storedConfig = getStoredAgentConfig()
const savedConfig = ref<StoredAgentConfig | null>(storedConfig)
const knownProviders = ['openai', 'anthropic', 'google_genai']
const initialProvider = storedConfig?.model_provider || 'openai'

const mode = ref<ModelMode>(storedConfig ? 'custom' : 'system')
const showApiKey = ref(false)
const customProvider = ref(knownProviders.includes(initialProvider) ? '' : initialProvider)
const form = reactive<StoredAgentConfig>({
  api_key: storedConfig?.api_key || '',
  api_base: storedConfig?.api_base || '',
  model_name: storedConfig?.model_name || 'gpt-4o',
  model_provider: knownProviders.includes(initialProvider) ? initialProvider : 'custom',
})

const modelOptions: ModelOption[] = [
  { value: 'gpt-4o', label: 'GPT-4o', provider: 'openai' },
  { value: 'gpt-4o-mini', label: 'GPT-4o mini', provider: 'openai' },
  { value: 'claude-opus-4-6', label: 'Claude Opus 4.6', provider: 'anthropic' },
  { value: CUSTOM_MODEL_VALUE, label: t('Custom model') },
]

const selectedModel = ref(
  modelOptions.some((option) => option.value === form.model_name)
    ? form.model_name
    : CUSTOM_MODEL_VALUE
)

const effectiveProvider = computed(() => {
  if (form.model_provider === 'custom') {
    return customProvider.value.trim()
  }
  return form.model_provider
})

function cleanValue(value?: string) {
  const trimmedValue = value?.trim()
  return trimmedValue || undefined
}

const statusText = computed(() => {
  if (mode.value === 'system') {
    return savedConfig.value ? t('Save to use system default for new chats') : t('New chats use system default')
  }
  const modelName = form.model_name?.trim()
  if (!modelName) {
    return t('New chats use custom model')
  }
  return isSavedCustomConfig.value
    ? t('New chats use {model}', { model: modelName })
    : t('Save to use {model} for new chats', { model: modelName })
})

const currentCustomConfig = computed<StoredAgentConfig>(() => ({
  api_key: cleanValue(form.api_key),
  api_base: cleanValue(form.api_base),
  model_name: cleanValue(form.model_name),
  model_provider: cleanValue(effectiveProvider.value),
}))

const isSavedCustomConfig = computed(() => {
  const saved = savedConfig.value
  const current = currentCustomConfig.value
  return Boolean(
    saved
    && saved.api_key === current.api_key
    && saved.api_base === current.api_base
    && saved.model_name === current.model_name
    && saved.model_provider === current.model_provider
  )
})

function handleModelSelect(value: any) {
  if (typeof value !== 'string') return
  selectedModel.value = value
  if (value === CUSTOM_MODEL_VALUE) {
    form.model_name = ''
    return
  }

  const selectedOption = modelOptions.find((option) => option.value === value)
  form.model_name = value
  if (selectedOption?.provider) {
    form.model_provider = selectedOption.provider
  }
}

function resetToSystem() {
  mode.value = 'system'
  selectedModel.value = 'gpt-4o'
  form.api_key = ''
  form.api_base = ''
  form.model_name = 'gpt-4o'
  form.model_provider = 'openai'
  customProvider.value = ''
  clearStoredAgentConfig()
  savedConfig.value = null
  showSuccessToast(t('Model settings reset'))
}

function saveConfig() {
  if (mode.value === 'system') {
    clearStoredAgentConfig()
    savedConfig.value = null
    showSuccessToast(t('Model settings saved'))
    return
  }

  const modelName = form.model_name?.trim()
  if (!modelName) {
    showErrorToast(t('Model name is required'))
    return
  }

  const provider = effectiveProvider.value
  if (!provider) {
    showErrorToast(t('Provider is required'))
    return
  }

  savedConfig.value = saveStoredAgentConfig({
    api_key: form.api_key,
    api_base: form.api_base,
    model_name: modelName,
    model_provider: provider,
  })
  showSuccessToast(t('Model settings saved'))
}
</script>
