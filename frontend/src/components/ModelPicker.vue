<template>
  <Select :model-value="selectedModelId" :disabled="disabled" @update:modelValue="handleChange">
    <SelectTrigger
      class="h-8 max-w-[220px] min-w-[128px] rounded-full px-3 text-xs text-[var(--text-secondary)]"
      :title="selectedOptionTitle"
    >
      <Bot class="size-4 flex-shrink-0 text-[var(--icon-secondary)]" />
      <SelectValue :placeholder="t('Select model')" />
    </SelectTrigger>
    <SelectContent :side-offset="6" class="min-w-[240px]">
      <SelectItem
        v-for="option in options"
        :key="option.id"
        :value="option.id"
      >
        <div class="flex flex-col">
          <span>{{ option.is_system_default ? t('System default') : option.label }}</span>
          <span class="text-[11px] text-[var(--text-tertiary)]">{{ option.model_name }}</span>
        </div>
      </SelectItem>
    </SelectContent>
  </Select>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import { Bot } from 'lucide-vue-next';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type { ChatModelOption } from '@/api/agentConfig';
import { SYSTEM_MODEL_ID } from '@/api/agentConfig';

const props = withDefaults(defineProps<{
  options: ChatModelOption[];
  selectedModelId?: string;
  disabled?: boolean;
}>(), {
  selectedModelId: SYSTEM_MODEL_ID,
  disabled: false,
});

const emit = defineEmits<{
  (e: 'update:selectedModelId', value: string): void;
}>();

const { t } = useI18n();

const selectedOption = computed(() => (
  props.options.find((option) => option.id === props.selectedModelId)
  || props.options[0]
));

const selectedOptionTitle = computed(() => {
  if (!selectedOption.value) {
    return t('Select model');
  }
  if (selectedOption.value.is_system_default) {
    return `${t('System default')}: ${selectedOption.value.model_name}`;
  }
  return selectedOption.value.label;
});

function handleChange(value: unknown) {
  if (typeof value === 'string') {
    emit('update:selectedModelId', value);
  }
}
</script>
