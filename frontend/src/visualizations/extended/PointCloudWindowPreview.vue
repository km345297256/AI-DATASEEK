<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { filePreviewIdentity, pluginPreviewIdentity } from '../previewIdentity';
import { requestVisualization } from '../runtime';
import { POINTCLOUD_WARNING, localPointGeometry, parsePointCloudWindow, pointCatalogIdentity, pointCoordinates, validatePointSelection, type PointCloudData } from './pointcloudWindowData';

const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const scope = usePreviewLoad(), sceneScope = usePreviewLoad();
const catalog = shallowRef<PointCloudData>(), displayed = shallowRef<PointCloudData>(), target = ref<HTMLDivElement>();
const pointOffset = ref(0), pointCount = ref(1024), inspected = ref(0), colorMode = ref('uniform'), busy = ref(false), error = ref('');
const current = computed(() => displayed.value ?? catalog.value);
const detail = computed(() => displayed.value && Number.isSafeInteger(inspected.value) && inspected.value >= 0 && inspected.value < displayed.value.metadata.output_points ? pointCoordinates(displayed.value, inspected.value) : undefined);
let version: string | undefined;
function envelope(result: Awaited<ReturnType<typeof requestVisualization>>, kind: 'tree' | 'geometry') {
  if (result.kind !== kind || !/^[0-9a-f]{64}$/.test(result.version) || JSON.stringify(result.warnings) !== JSON.stringify([POINTCLOUD_WARNING]) || typeof result.sampled !== 'boolean') throw new Error('LAS 结果或版本无效。');
}
function clearWindow() { scope.begin(); sceneScope.begin(); displayed.value = undefined; busy.value = false; error.value = ''; }
async function inspect() {
  const request = scope.begin(); sceneScope.begin(); catalog.value = undefined; displayed.value = undefined; error.value = ''; busy.value = false; version = undefined;
  await nextTick(); if (!request.isCurrent() || !props.plugin.enabled) return;
  busy.value = true;
  try {
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'tree' }, request.signal);
    request.assertCurrent(); envelope(result, 'tree');
    const parsed = parsePointCloudWindow('tree', result.payload, result.metadata);
    if (result.sampled || parsed.metadata.source_bytes !== props.file.size) throw new Error('LAS 文件大小或目录声明变化。');
    version = result.version; pointOffset.value = 0; pointCount.value = Math.min(1024, parsed.metadata.total_points); colorMode.value = 'uniform'; catalog.value = parsed;
  } catch (reason) { if (request.isCurrent()) error.value = reason instanceof Error ? reason.message : 'LAS 目录读取失败。'; }
  finally { if (request.isCurrent()) busy.value = false; }
}
async function draw(data: PointCloudData) {
  const request = sceneScope.begin(), local = localPointGeometry(data);
  const [THREE, { OrbitControls }] = await Promise.all([import('three'), import('three/addons/controls/OrbitControls.js')]);
  await nextTick(); request.assertCurrent(); if (!target.value) return;
  const element = document.createElement('div'); element.style.cssText = 'width:100%;height:440px;position:relative'; element.dataset.testid = 'pointcloud-scene'; target.value.replaceChildren(element);
  request.onDispose(() => element.remove());
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: true });
  request.onDispose(() => { renderer.dispose(); renderer.forceContextLoss(); });
  const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(45, 1, .001, 100);
  const geometry = new THREE.BufferGeometry(), material = new THREE.PointsMaterial({ size: 5, sizeAttenuation: false, vertexColors: true });
  let controls: InstanceType<typeof OrbitControls> | undefined, resize: ResizeObserver | undefined;
  request.onDispose(() => { resize?.disconnect(); controls?.dispose(); geometry.dispose(); material.dispose(); });
  renderer.outputColorSpace = THREE.LinearSRGBColorSpace; renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x0f172a, 1); element.appendChild(renderer.domElement);
  geometry.setAttribute('position', new THREE.BufferAttribute(local.positions, 3));
  const colors = new Float32Array(data.raw.length);
  for (let i = 0; i < colors.length / 3; i++) {
    let rgb = [.15, .8, .65];
    if (colorMode.value === 'rgb' && data.rgb) rgb = data.rgb.slice(i * 3, i * 3 + 3).map(v => v / 65535);
    if (colorMode.value === 'intensity') rgb = [0, 1, 2].map(() => data.intensity[i]! / 65535);
    if (colorMode.value === 'classification') { const c = data.classification[i]!; rgb = [((c * 71 + 45) % 192 + 63) / 255, ((c * 137 + 29) % 192 + 63) / 255, ((c * 193 + 77) % 192 + 63) / 255]; }
    colors.set(rgb, i * 3);
  }
  geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3)); geometry.computeBoundingSphere(); geometry.computeBoundingBox();
  const points = new THREE.Points(geometry, material); scene.add(points);
  const center = geometry.boundingSphere!.center, radius = Math.max(geometry.boundingSphere!.radius, .1);
  camera.up.set(0, 0, 1); camera.position.copy(center).add(new THREE.Vector3(1.6, -2, 1.7).multiplyScalar(radius * 1.5)); camera.lookAt(center);
  controls = new OrbitControls(camera, renderer.domElement); controls.target.copy(center); controls.enableDamping = false; controls.update();
  const render = () => { if (request.isCurrent()) renderer.render(scene, camera); };
  controls.addEventListener('change', render);
  const updateSize = () => { if (!request.isCurrent()) return; const width = Math.max(1, Math.min(element.clientWidth, 1200)); renderer.setSize(width, 440); camera.aspect = width / 440; camera.updateProjectionMatrix(); render(); };
  resize = new ResizeObserver(updateSize); resize.observe(element); updateSize();
  const ray = new THREE.Raycaster(); ray.params.Points!.threshold = Math.max(radius * .025, .005);
  const pointer = (event: PointerEvent) => { if (!request.isCurrent()) return; const rect = renderer.domElement.getBoundingClientRect(); if (!rect.width || !rect.height) return;
    ray.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1), camera);
    const hit = ray.intersectObject(points)[0]; if (hit?.index !== undefined) inspected.value = hit.index; };
  renderer.domElement.addEventListener('pointermove', pointer); request.onDispose(() => renderer.domElement.removeEventListener('pointermove', pointer));
}
async function loadWindow() {
  const request = scope.begin(); sceneScope.begin(); displayed.value = undefined; error.value = ''; busy.value = true;
  try {
    if (!props.plugin.enabled || !catalog.value || !version) throw new Error('请先读取 LAS 目录。');
    const selected = validatePointSelection({ point_offset: pointOffset.value, point_count: pointCount.value }, catalog.value.metadata.total_points), pinned = version;
    const result = await requestVisualization(props.file, props.plugin, 'preview', { kind: 'geometry', version: pinned, ...selected }, request.signal);
    request.assertCurrent(); envelope(result, 'geometry');
    const parsed = parsePointCloudWindow('geometry', result.payload, result.metadata, selected);
    if (result.version !== pinned || pointCatalogIdentity(parsed.metadata) !== pointCatalogIdentity(catalog.value.metadata) || result.sampled !== (selected.point_count < parsed.metadata.total_points)) throw new Error('LAS 点窗口与已检查文件或目录不一致。');
    inspected.value = 0; await draw(parsed); request.assertCurrent(); displayed.value = parsed;
  } catch (reason) { if (request.isCurrent()) { sceneScope.begin(); error.value = reason instanceof Error ? reason.message : 'LAS 窗口读取失败。'; } }
  finally { if (request.isCurrent()) busy.value = false; }
}
watch([pointOffset, pointCount], () => { if (catalog.value) clearWindow(); });
watch(colorMode, () => { const value = displayed.value, color = colorMode.value; if (value) void draw(value).catch(reason => {
  if (reason instanceof DOMException && reason.name === 'AbortError') return;
  if (props.plugin.enabled && displayed.value === value && colorMode.value === color) { sceneScope.begin(); error.value = reason instanceof Error ? reason.message : '点云重绘失败。'; }
}); });
watch([() => filePreviewIdentity(props.file), () => pluginPreviewIdentity(props.plugin)], () => { void inspect(); }, { immediate: true });
</script>

<template>
  <section class="pointcloud-preview">
    <p role="note">{{ POINTCLOUD_WARNING }}</p>
    <p class="notice">只读试点：未压缩 LAS 1.2 / 1.4，点格式 0–3、6–8；不支持 LAZ、COPC、波形。额外维度仅跳过，不解码。CRS 元记录仅识别是否存在；单位始终未知，不叠加在线地图。包括 withheld 点在内的所有所选记录均保留。</p>
    <button class="reload" :disabled="busy" @click="inspect">重新读取 LAS 目录</button>
    <form v-if="catalog" @submit.prevent="loadWindow">
      <label>点记录起点（从 0 开始）<input v-model.number="pointOffset" aria-label="LAS 点记录起点" type="number" min="0" step="1" :disabled="busy" /></label>
      <label>连续点数<input v-model.number="pointCount" aria-label="LAS 连续点数" type="number" min="1" max="16384" step="1" :disabled="busy" /></label>
      <button type="submit" :disabled="busy || !catalog.metadata.total_points">读取 LAS 点窗口</button>
    </form>
    <p v-if="catalog && !displayed && !busy" class="notice">目录未读取点数据。全文件声明 {{ catalog.metadata.total_points }} 点；请选择连续记录窗口，最多 16,384 点。{{ catalog.metadata.total_points ? '' : '文件为空点云。' }}</p>
    <p v-if="busy" role="status">正在读取授权 LAS 范围…</p><p v-if="error" role="alert">{{ error }}</p>
    <p v-if="current" class="notice" data-testid="pointcloud-stats">LAS {{ current.metadata.las_version }} / 点格式 {{ current.metadata.point_format }}；本次读取 {{ current.metadata.read_bytes }} / {{ current.metadata.source_bytes }} 字节，{{ current.metadata.read_requests }} 次；CRS {{ current.metadata.crs_declarations.length ? '存在声明但未解析' : '未声明' }}，单位未知。{{ displayed ? `仅本窗 ${displayed.metadata.output_points} 点，不代表全文件分布。` : '' }}</p>
    <div ref="target" class="scene" aria-label="LAS 本地三维点云" />
    <div v-if="displayed" class="details">
      <label>着色（仅显示）<select v-model="colorMode" aria-label="LAS 着色"><option value="uniform">统一颜色</option><option value="classification">分类代码</option><option value="intensity">强度 / 65535</option><option v-if="displayed.rgb" value="rgb">原始 RGB / 65535</option></select></label>
      <p class="notice">局部整数原点＋公共显示比例用于三维渲染；XYZ 原值不变，强度/RGB 不自动拉伸或标定。拖动旋转、滚轮缩放、悬停查看点。头部全局范围只是文件声明，未全文件核验。</p>
      <p data-testid="pointcloud-window-bounds">本窗 XYZ 范围：{{ displayed.metadata.window_bounds.map((v: number[]) => v.join(' … ')).join('；') }}</p>
      <label>本窗点索引<input v-model.number="inspected" aria-label="LAS 本窗点索引" type="number" min="0" :max="displayed.metadata.output_points - 1" step="1" /></label>
      <p v-if="detail" data-testid="pointcloud-point-detail">原始整数 {{ detail.raw.join(', ') }}；XYZ（单位未知）{{ detail.xyz.join(', ') }}；强度 {{ displayed.intensity[inspected] }}；分类 {{ displayed.classification[inspected] }}；分类标志位 {{ displayed.flags[inspected] }}（1 synthetic / 2 key-point / 4 withheld / 8 overlap）。比例 {{ displayed.metadata.scales.join(', ') }}；偏移 {{ displayed.metadata.offsets.join(', ') }}。</p>
    </div>
  </section>
</template>
<style scoped>
.pointcloud-preview{display:flex;flex:1;flex-direction:column;gap:10px;overflow:auto;padding:16px;min-height:0;font-size:13px}p{margin:0;line-height:1.6}.notice{font-size:12px;color:#667085}.reload{align-self:flex-start}form,.details{display:flex;gap:12px;flex-wrap:wrap;padding:12px;border:1px solid #d0d5dd;border-radius:6px}.details p{width:100%}label{display:flex;gap:6px;align-items:center}input{width:110px}input,select,button{padding:6px 8px;border:1px solid #ccd3db;border-radius:4px}button:disabled{opacity:.5}[role=alert]{color:#b42318}.scene{width:100%;min-height:0;flex-shrink:0}
</style>
