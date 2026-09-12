<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import type { FileInfo } from '../../api/file';
import type { VisualizationPlugin } from '../contract';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { requestPreview } from './runtime';
import { parseGeoData, rasterPixels } from './geoFormatData';
import { displayError } from './domains/lifecycle';
import 'ol/ol.css';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const target = ref<HTMLElement>(), busy = ref(false), error = ref(''), crs = ref(''), version = ref<string>();
const metadata = ref<Record<string, unknown>>({}), warnings = ref<string[]>([]), status = ref('');
const range = ref<{ min: number | null; max: number | null; invalid: number }>();
const kml = computed(() => /\.kml$/i.test(props.file.filename));
const loads = usePreviewLoad();
async function render() {
  const pinned = version.value, requestedCrs = kml.value ? 'EPSG:4326' : crs.value || 'unknown';
  const load = loads.begin(); busy.value = true; error.value = ''; range.value = undefined; status.value = ''; metadata.value = {}; warnings.value = [];
  try {
    if (!props.plugin.enabled) return;
    const result = await requestPreview(props.file, props.plugin, { kind: 'map', ...(pinned ? { version: pinned } : {}), ...(!kml.value && requestedCrs !== 'unknown' ? { crs: requestedCrs } : {}) }, load.signal);
    load.assertCurrent(); const data = parseGeoData(result);
    if (pinned && result.version !== pinned) throw new Error('文件版本变化，请重新打开地理预览。');
    if (data.metadata.crs !== requestedCrs) throw new Error('地理响应坐标系与显式选择不一致。');
    const [{ default: Map }, { default: View }] = await Promise.all([import('ol/Map.js'), import('ol/View.js')]); load.assertCurrent();
    let projection: string | import('ol/proj/Projection.js').default = String(data.metadata.crs);
    if (projection === 'unknown') {
      const { default: Projection } = await import('ol/proj/Projection.js'); load.assertCurrent();
      // Pixel units have no default metre conversion in OpenLayers. Supply a
      // bounded local extent and unit scale, never a guessed geographic CRS.
      projection = new Projection({ code: 'local-grid-preview', units: 'pixels', metersPerUnit: 1,
        extent: [0, 0, data.array!.shape[1]!, data.array!.shape[0]!] });
    }
    const map = new Map({ target: target.value, layers: [], view: new View({ projection, center: [0, 0], zoom: 1 }) });
    load.onDispose(() => { map.setTarget(undefined); map.dispose(); });
    const observer = new ResizeObserver(() => map.updateSize()); observer.observe(target.value!); load.onDispose(() => observer.disconnect());
    if (data.geojson) {
      const [{ default: GeoJSON }, { default: VectorSource }, { default: VectorLayer }] = await Promise.all([import('ol/format/GeoJSON.js'), import('ol/source/Vector.js'), import('ol/layer/Vector.js')]); load.assertCurrent();
      const source = new VectorSource({ features: new GeoJSON().readFeatures(data.geojson, { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:4326' }) });
      const layer = new VectorLayer({ source }); map.addLayer(layer);
      load.onDispose(() => { layer.setSource(null); layer.dispose(); source.clear(); source.dispose(); });
      const featureExtent = source.getExtent(); if (!featureExtent) throw new Error('地理要素范围为空。');
      map.getView().fit(featureExtent, { padding: [32, 32, 32, 32], maxZoom: 16 });
      status.value = `${data.geojson.features.length} 个要素 · WGS84 经度/纬度 · 无网络底图`;
    } else {
      const array = data.array!, height = array.shape[0]!, width = array.shape[1]!;
      const colored = rasterPixels(array.values), canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
      load.onDispose(() => { canvas.width = 0; canvas.height = 0; });
      const context = canvas.getContext('2d'); if (!context) throw new Error('无法创建栅格画布。');
      const pixels = context.createImageData(width, height); pixels.data.set(colored.pixels); context.putImageData(pixels, 0, 0);
      const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(value => value ? resolve(value) : reject(new Error('栅格编码失败。')))); load.assertCurrent(); canvas.width = 0; canvas.height = 0;
      const url = URL.createObjectURL(blob); load.onDispose(() => URL.revokeObjectURL(url));
      const [{ default: ImageLayer }, { default: ImageStatic }] = await Promise.all([import('ol/layer/Image.js'), import('ol/source/ImageStatic.js')]); load.assertCurrent();
      const extent = data.metadata.crs === 'unknown' ? [0, 0, width, height] : data.metadata.extent as number[];
      const source = new ImageStatic({ url, projection, imageExtent: extent, interpolate: false }), layer = new ImageLayer({ source }); map.addLayer(layer);
      load.onDispose(() => { layer.setSource(null); layer.dispose(); source.dispose(); });
      map.getView().fit(extent, { padding: [24, 24, 24, 24] });
      range.value = { min: colored.min, max: colored.max, invalid: colored.invalid };
      status.value = `${width}×${height} 最近邻预览 · ${data.metadata.crs === 'unknown' ? '纯数值热图（非地理定位）' : data.metadata.crs} · 色标仅代表预览样本`;
    }
    metadata.value = data.metadata; warnings.value = result.warnings as string[]; version.value = result.version as string;
  } catch (reason) { if (load.isCurrent()) error.value = displayError(reason); }
  finally { if (load.isCurrent()) busy.value = false; }
}
function reset() { version.value = undefined; crs.value = ''; metadata.value = {}; warnings.value = []; void render(); }
watch([() => props.file.file_id, () => props.plugin.id, () => props.plugin.version, () => props.plugin.enabled], reset);
onMounted(() => { void render(); });
</script>
<template><section class="geo-preview">
  <form v-if="!kml" class="toolbar" @submit.prevent="render"><label>坐标系 <select v-model="crs" :disabled="busy"><option value="">未指定（纯数值热图）</option><option>EPSG:4326</option><option>EPSG:3857</option></select></label><button :disabled="busy">应用坐标系</button><span>仅按数据说明明确选择，不自动推测。</span></form>
  <p v-if="busy" role="status">正在隔离环境读取…</p><p v-if="error" role="alert">{{ error }}</p>
  <div v-if="range" class="legend"><template v-if="range.min !== null"><span>{{ range.min?.toPrecision(5) }}</span><span class="ramp"/><span>{{ range.max?.toPrecision(5) }}</span></template><span v-else>无有效数值</span><span>NoData / 无效样本 {{ range.invalid }} 个</span></div>
  <div ref="target" class="surface" :aria-busy="busy"/><p class="notice">{{ status }}</p>
  <p v-if="metadata.extent" class="notice">原始坐标范围（图像外边界）：{{ metadata.extent }}；网格定位：{{ metadata.registration }}。</p>
  <p v-for="warning in warnings" :key="warning" class="notice">{{ warning }}</p>
</section></template>
<style scoped>.geo-preview{display:flex;flex-direction:column;height:100%;min-height:360px}.surface{flex:1;min-height:300px;background:#eef2f5}.toolbar,.legend{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:8px;font-size:12px}button,select{border:1px solid #ccd3db;border-radius:4px;padding:5px 8px}button:disabled{opacity:.5}.ramp{width:140px;height:12px;background:linear-gradient(90deg,rgb(0,190,255),rgb(255,0,0))}.notice{font-size:12px;padding:6px 8px;color:#667085}[role=alert]{color:#b42318;padding:8px}</style>
