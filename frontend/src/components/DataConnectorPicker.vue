<template>
  <Dialog v-model:open="open">
    <DialogContent class="w-[min(520px,calc(100vw-24px))]">
      <DialogHeader><DialogTitle>选择数据连接器</DialogTitle></DialogHeader>
      <div class="space-y-4 px-6 pb-6">
        <p class="text-sm text-[var(--text-secondary)]">选择本次任务的数据来源，再在对话中描述需要查找或读取的数据。</p>
        <p v-if="loading" role="status">正在加载…</p>
        <p v-else-if="error" role="alert">{{ error }} <button class="underline" @click="load">重试</button></p>
        <p v-else-if="!catalog.length" class="text-sm">暂无可用的数据连接器。</p>
        <div class="max-h-[45vh] space-y-3 overflow-y-auto">
          <label v-for="item in catalog" :key="item.id" class="flex cursor-pointer items-start gap-3 rounded-xl border border-[var(--border-main)] p-4">
            <input type="checkbox" class="mt-1" :checked="selected.includes(item.id)" :disabled="disabled" @change="toggle(item.id)" />
            <span class="min-w-0">
              <span class="block font-medium">{{ item.title }}</span>
              <span class="mt-1 block text-sm text-[var(--text-secondary)]">{{ item.description }}</span>
              <span class="mt-2 block text-xs">{{ item.operations.map(op => op.title).join(' · ') }}</span>
              <span v-for="limit in item.limitations" :key="limit" class="mt-1 block text-xs text-[var(--text-tertiary)]">{{ limit }}</span>
            </span>
          </label>
        </div>
        <div class="flex items-center justify-between">
          <RouterLink to="/plugins?tab=connectors" class="text-sm underline" @click="open = false">管理连接器</RouterLink>
          <button type="button" class="rounded-lg bg-[var(--Button-primary-black)] px-4 py-2 text-sm text-[var(--text-onblack)]" @click="open = false">完成</button>
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>
<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { listDataConnectors, type DataConnector } from '@/api/dataConnector';
const props = defineProps<{ open: boolean; selected: string[]; disabled?: boolean }>();
const emit = defineEmits<{ (event: 'update:open', value: boolean): void; (event: 'update:selected', value: string[]): void; (event: 'catalog', value: DataConnector[]): void }>();
const open = computed({ get: () => props.open, set: value => emit('update:open', value) });
const catalog = ref<DataConnector[]>([]), error = ref(''), loading = ref(false);
async function load() {
  loading.value = true; error.value = '';
  try { catalog.value = await listDataConnectors(); emit('catalog', catalog.value); }
  catch { error.value = '无法读取连接器目录。'; }
  finally { loading.value = false; }
}
function toggle(id: string) {
  if (props.disabled) return;
  emit('update:selected', props.selected.includes(id) ? props.selected.filter(item => item !== id) : [...props.selected, id]);
}
watch(open, value => { if (value) void load(); });
</script>
