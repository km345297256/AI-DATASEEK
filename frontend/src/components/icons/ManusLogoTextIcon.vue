<template>
  <span
    class="inline-flex items-center gap-1.5 font-semibold leading-none text-[var(--logo-color)]"
    :style="{ minWidth: cssWidth, height: cssHeight, fontSize }"
    aria-label="DataSeek"
  >
    <img
      src="/ai-dataseek-logo.png"
      alt=""
      class="shrink-0 object-contain"
      :style="{ width: iconSize, height: iconSize }"
    />
    <span class="whitespace-nowrap">DataSeek</span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps({
  width: {
    type: [String, Number],
    default: 88,
  },
  height: {
    type: [String, Number],
    default: 38,
  },
})

const toCssLength = (value: string | number) => {
  if (typeof value === 'number') return `${value}px`
  return /^\d+(\.\d+)?$/.test(value) ? `${value}px` : value
}

const cssWidth = computed(() => toCssLength(props.width))
const cssHeight = computed(() => toCssLength(props.height))
const heightNumber = computed(() => Number(props.height) || 30)
const iconSize = computed(() => `${heightNumber.value}px`)
const fontSize = computed(() => `${Math.max(13, Math.round(heightNumber.value * 0.5))}px`)
</script>
