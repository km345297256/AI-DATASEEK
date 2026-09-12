<template>
  <div class="space-y-3 text-sm">
    <p class="text-xs text-gray-600">{{ UGRID_WARNING }}</p>
    <p class="text-xs text-gray-600">只读试点：UGRID 1.0 / NetCDF4 根组，二维节点/面场；先验证目录，再完整读取有界拓扑。仅原生坐标平面，不表示距离或面积；声明经度跨度超过 180° 时拒绝。固定长度元数据不适用时，请选原始 HDF5 查看器检查，不会自动切换。</p>
    <button type="button" @click="loadTree">重新读取 UGRID 目录</button>
    <p v-if="error" role="alert" class="text-amber-700">{{ error }}</p>
    <div v-if="directory" class="flex flex-wrap items-center gap-3 rounded border p-3">
      <label>网格 <select v-model="meshId" aria-label="UGRID 网格"><option v-for="m in directory.meshes" :key="m.id" :value="m.id">{{ m.label }} · {{ m.node_count }} 节点 / {{ m.face_count }} 面</option></select></label>
      <label>场 <select v-model="fieldId" aria-label="UGRID 场"><option value="">仅拓扑线框</option><option v-for="f in mesh?.fields" :key="f.id" :value="f.id">{{ f.label }} · {{ f.location === 'node' ? '节点' : '面' }}</option></select></label>
      <label v-for="(d, i) in dimensions" :key="d.label">{{ d.label }} 索引 <input v-model.number="indices[i]" type="number" min="0" :max="d.size - 1" :aria-label="`UGRID ${d.label} 索引`" class="w-20 rounded border" /></label>
      <button type="button" :disabled="busy" @click="loadGeometry">读取 UGRID 网格</button>
    </div>
    <p v-if="field" data-testid="ugrid-storage-warning" class="rounded border border-amber-200 bg-amber-50 p-2">{{ field.label }}：原始存储值，未应用 CF 标定；源声明的物理单位 {{ field.unit ?? '未知' }}（当前未应用）。scale_factor={{ field.scale_factor ?? '未声明' }}，add_offset={{ field.add_offset ?? '未声明' }}。{{ field.location === 'node' ? '节点值只绘制在节点上，不向面插值。' : '面值按原多边形平涂，不向节点插值。' }}</p>
    <p v-if="displayed" data-testid="ugrid-stats">完整拓扑 {{ mesh?.node_count }} 节点 / {{ mesh?.face_count }} 面；取回 {{ displayed.metadata.read_bytes }} / {{ displayed.metadata.source_bytes }} 字节，{{ displayed.metadata.read_requests }} 次范围读取；缺失场值 {{ displayed.metadata.missing_values }}。横轴 {{ mesh?.coordinates[0]?.label }} [{{ mesh?.coordinates[0]?.unit ?? '单位未知' }}]，纵轴 {{ mesh?.coordinates[1]?.label }} [{{ mesh?.coordinates[1]?.unit ?? '单位未知' }}]，保持原坐标比例。</p>
    <p v-if="colorRange" data-testid="ugrid-color-range">颜色范围（当前所选原始存储值）：{{ colorRange[0] }} — {{ colorRange[1] }}；蓝色低、红色高，灰色为缺失。常数场使用中间色。</p>
    <div ref="target" data-testid="ugrid-scene" class="h-[420px] w-full overflow-hidden rounded border" aria-label="UGRID 原生坐标平面" />
    <p v-if="displayed?.geometry" data-testid="ugrid-values">所选原始场值（前 16 项）：{{ displayed.geometry.values?.slice(0, 16).map(v => v === null ? '缺失' : v).join(', ') ?? '仅拓扑' }}</p>
    <div v-if="detail" class="flex flex-wrap gap-3"><label>本次{{ detail.location }}索引 <input v-model.number="inspectIndex" type="number" min="0" :max="inspectMaximum" aria-label="UGRID 检查索引" class="w-24 rounded border" /></label><p data-testid="ugrid-detail">{{ detail.text }}</p></div>
    <p v-if="busy" role="status">正在读取受控选区…</p>
  </div>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { parseUgridWindow, ugridCanvasPoints, validateUgridSelection, UGRID_WARNING, type UgridData, type UgridSelection } from './ugridWindowData';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), target = ref<HTMLDivElement>(), error = ref(''), busy = ref(false), directory = ref<UgridData>(), displayed = ref<UgridData>(), version = ref('');
const meshId = ref(''), fieldId = ref(''), indices = ref<number[]>([]);
const inspectIndex = ref(0);
const mesh = computed(() => directory.value?.meshes.find(m => m.id === meshId.value));
const field = computed(() => mesh.value?.fields.find(f => f.id === fieldId.value));
const dimensions = computed(() => field.value?.dimensions.filter((_, i) => i !== field.value?.spatial_axis) ?? []);
const colorRange = computed(() => { const values = displayed.value?.geometry?.values?.filter((v): v is number => v !== null) ?? []; return values.length ? [Math.min(...values), Math.max(...values)] : null; });
const inspectMaximum = computed(() => { const geometry = displayed.value?.geometry; return geometry ? (geometry.location === 'node' ? geometry.coordinates.length / 2 : geometry.faces.length) - 1 : 0; });
const detail = computed(() => { const geometry = displayed.value?.geometry; if (!geometry) return null; const index = Math.max(0, Math.min(inspectMaximum.value, Number.isInteger(inspectIndex.value) ? inspectIndex.value : 0));
  const location = geometry.location === 'node' ? '节点' : '面', coordinates = geometry.location === 'node' ? geometry.coordinates.slice(index * 2, index * 2 + 2).join(', ') : geometry.faces[index]!.join(', ');
  return { location, text: `${location} ${index}；${geometry.location === 'node' ? '原始坐标' : '节点索引（0 起点）'}：${coordinates}；原始值：${geometry.values ? geometry.values[index] === null ? '缺失' : geometry.values[index] : '未选择场'}` }; });
function clearSelection() { scope.begin(); displayed.value = undefined; busy.value = false; error.value = ''; }
watch(meshId, () => { clearSelection(); fieldId.value = ''; indices.value = []; });
watch(fieldId, () => { clearSelection(); indices.value = dimensions.value.map(() => 0); });
watch(() => JSON.stringify(indices.value), () => clearSelection());
async function loadTree() {
  const load = scope.begin(); directory.value = undefined; displayed.value = undefined; version.value = ''; error.value = ''; busy.value = !!props.plugin.enabled;
  if (!props.plugin.enabled) return;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree' }, load.signal); load.assertCurrent();
    if (result.kind !== 'tree' || result.sampled !== false || result.warnings.length !== 1 || result.warnings[0] !== UGRID_WARNING) throw new Error('目录响应无效');
    const parsed = parseUgridWindow('tree', result.payload, result.metadata);
    if (parsed.metadata.source_bytes !== props.file.size) throw new Error('文件大小已变化');
    directory.value = parsed; version.value = result.version; meshId.value = parsed.meshes[0]!.id; fieldId.value = ''; indices.value = [];
  } catch (e) { if (load.isCurrent()) error.value = e instanceof Error ? e.message : '无法读取 UGRID 目录。'; }
  finally { if (load.isCurrent()) busy.value = false; }
}
async function loadGeometry() {
  if (!props.plugin.enabled || !directory.value || !version.value || !mesh.value) return;
  const load = scope.begin(); displayed.value = undefined; busy.value = true; error.value = '';
  try {
    const selection: UgridSelection = { mesh: meshId.value, field: fieldId.value || null, indices: [...indices.value] }; validateUgridSelection(selection);
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'geometry', version: version.value, ...selection }, load.signal); load.assertCurrent();
    if (result.kind !== 'geometry' || result.version !== version.value || result.sampled !== false || result.warnings.length !== 1 || result.warnings[0] !== UGRID_WARNING) throw new Error('网格版本或语义已变化');
    const parsed = parseUgridWindow('geometry', result.payload, result.metadata, selection);
    if (parsed.metadata.source_bytes !== props.file.size || JSON.stringify(parsed.meshes) !== JSON.stringify(directory.value.meshes)) throw new Error('网格目录或源文件已变化');
    displayed.value = parsed; inspectIndex.value = 0;
    const host = target.value, geometry = parsed.geometry; if (!host || !geometry) throw new Error('绘图区不可用');
    const canvas = document.createElement('canvas'); load.onDispose(() => { canvas.width = canvas.height = 0; canvas.remove(); });
    const context = canvas.getContext('2d'); if (!context) throw new Error('Canvas 不可用');
    host.replaceChildren(canvas); canvas.style.width = '100%'; canvas.style.height = '100%';
    function draw() {
      if (!load.isCurrent() || !host || !geometry || !context) return;
      const width = Math.max(1, host.clientWidth), height = Math.max(1, host.clientHeight), ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); context.setTransform(ratio, 0, 0, ratio, 0, 0); context.fillStyle = '#f6f8fa'; context.fillRect(0, 0, width, height);
      const points = ugridCanvasPoints(geometry, width, height), finite = geometry.values?.filter((v): v is number => v !== null) ?? [], low = Math.min(...finite), high = Math.max(...finite);
      function color(v: number | null | undefined) { return v === null ? '#b5b5b5' : v === undefined ? '#eef4f2' : `hsl(${(1 - (high === low ? .5 : (v - low) / (high - low))) * 240},70%,45%)`; }
      geometry.faces.forEach((face, i) => { context.beginPath(); face.forEach((node, j) => { const p = points[node]!; if (j) context.lineTo(p[0], p[1]); else context.moveTo(p[0], p[1]); }); context.closePath(); if (geometry.location === 'face') { context.fillStyle = color(geometry.values?.[i]); context.fill(); } context.strokeStyle = '#536879'; context.lineWidth = 1; context.stroke(); });
      if (geometry.location === 'node') points.forEach((p, i) => { context.beginPath(); context.arc(p[0], p[1], 4, 0, Math.PI * 2); context.fillStyle = color(geometry.values?.[i]); context.fill(); });
      canvas.dataset.ugridLocation = geometry.location ?? 'topology'; canvas.dataset.ugridFaces = String(geometry.faces.length);
    }
    const observer = new ResizeObserver(draw); load.onDispose(() => observer.disconnect()); observer.observe(host); draw();
  } catch (e) { if (load.isCurrent()) { scope.begin(); displayed.value = undefined; error.value = e instanceof Error ? e.message : '无法读取 UGRID 网格。'; busy.value = false; } }
  finally { if (load.isCurrent()) busy.value = false; }
}
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => void loadTree(), { immediate: true });
</script>
