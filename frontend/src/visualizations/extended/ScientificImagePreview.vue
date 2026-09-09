<template>
  <section class="flex min-h-0 flex-1 flex-col overflow-auto p-4">
    <form v-if="plugin.reader === 'metpy'" class="mb-4 flex flex-wrap items-end gap-3 text-sm" @submit.prevent="loadImage">
      <label v-for="item in columns" :key="item.key">{{ item.label }}列名<input v-model="options[item.key]" required maxlength="128" class="block rounded border p-2" /><select v-model="options[item.unit]" class="mt-1 border p-1"><option v-for="unit in item.units" :key="unit">{{ unit }}</option></select></label>
      <button class="rounded border px-4 py-2" :disabled="busy">生成探空图</button>
    </form>
    <div v-else class="mb-4 flex items-center gap-3 text-sm"><label>分子序号（从0开始） <input v-model.number="molecule" type="number" min="0" max="99" class="w-20 border p-2" /></label><button @click="loadImage" :disabled="busy">读取结构</button></div>
    <p v-if="busy" role="status">正在隔离环境中生成图像…</p><p v-if="error" role="alert" class="text-amber-700">{{ error }}</p>
    <p v-if="plugin.reader === 'metpy' && !imageUrl" class="text-sm text-gray-500">请依据数据说明明确填写列名和单位；系统不会推测温标或气压单位。</p>
    <img v-if="imageUrl" :src="imageUrl" class="max-w-full self-center" :alt="plugin.name" />
    <dl class="mt-4 grid grid-cols-2 gap-2 text-sm"><template v-for="(value, key) in metadata" :key="key"><dt>{{ key }}</dt><dd class="break-words">{{ value }}</dd></template></dl>
    <p v-for="warning in warnings" :key="warning" class="mt-2 text-xs text-amber-700">{{ warning }}</p>
  </section>
</template>
<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestPreview } from './runtime';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const columns = [ { key: 'pressure_column', label: '气压', unit: 'pressure_unit', units: ['hPa', 'Pa'] }, { key: 'temperature_column', label: '温度', unit: 'temperature_unit', units: ['degC', 'K'] }, { key: 'dewpoint_column', label: '露点', unit: 'dewpoint_unit', units: ['degC', 'K'] } ];
const options = reactive<Record<string, string>>({ pressure_column: '', temperature_column: '', dewpoint_column: '', pressure_unit: 'hPa', temperature_unit: 'degC', dewpoint_unit: 'degC' });
const molecule = ref(0), imageUrl = ref(''), busy = ref(false), error = ref(''), metadata = ref<Record<string, unknown>>({}), warnings = ref<string[]>([]);
const loads = usePreviewLoad();
async function loadImage() {
  const load = loads.begin(); busy.value = true; error.value = ''; imageUrl.value = '';
  try {
    const result = await requestPreview(props.file, props.plugin, props.plugin.reader === 'metpy' ? { ...options } : { molecule: molecule.value }, load.signal);
    load.assertCurrent();
    if (result.media_type !== 'image/png' || typeof result.data_base64 !== 'string' || result.data_base64.length > 8 * 1024 * 1024) throw new Error('图像不符合插件协议。');
    const bytes = Uint8Array.from(atob(result.data_base64), c => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: 'image/png' }));
    load.onDispose(() => URL.revokeObjectURL(url)); imageUrl.value = url;
    metadata.value = result.metadata as Record<string, unknown>; warnings.value = result.warnings as string[];
  } catch (e) { if (load.isCurrent()) error.value = e instanceof Error ? e.message : '无法生成图像。'; }
  finally { if (load.isCurrent()) busy.value = false; }
}
onMounted(() => { if (props.plugin.reader === 'rdkit') void loadImage(); });
</script>
