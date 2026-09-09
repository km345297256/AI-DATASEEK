<script setup lang="ts">
import { onMounted, ref } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { loadPluginBytes } from '../runtime';
import { boundedGeoJSON, validateRaster } from './guards';
import { displayError, useDomainScope } from './lifecycle';
import 'ol/ol.css';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const target = ref<HTMLElement>(); const error = ref(''); const status = ref('读取本地数据…');
const scope = useDomainScope();
onMounted(async () => {
  try {
    const bytes = await loadPluginBytes(props.file, props.plugin, scope.signal); scope.check();
    const [{ default: Map }, { default: View }, { default: VectorLayer }, { default: VectorSource }, { default: GeoJSON }] = await Promise.all([import('ol/Map.js'), import('ol/View.js'), import('ol/layer/Vector.js'), import('ol/source/Vector.js'), import('ol/format/GeoJSON.js')]); scope.check();
    const map = new Map({ target: target.value, layers: [], view: new View({ center: [0, 0], zoom: 2 }) });
    scope.add(() => { map.setTarget(undefined); map.dispose(); });
    const observer = new ResizeObserver(() => map.updateSize()); observer.observe(target.value!); scope.add(() => observer.disconnect());
    if (/\.(geojson|json)$/i.test(props.file.filename)) {
      const data = boundedGeoJSON(bytes);
      const source = new VectorSource({ features: new GeoJSON().readFeatures(data, { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' }) });
      map.addLayer(new VectorLayer({ source }));
      map.getView().fit(source.getExtent()!, { padding: [32, 32, 32, 32], maxZoom: 16 });
      status.value = `${data.features.length} 个要素 · WGS84 · 离线无底图网络请求`;
    } else {
      const [{ fromArrayBuffer }, { default: ImageLayer }, { default: ImageStatic }, { transformExtent }] = await Promise.all([import('geotiff'), import('ol/layer/Image.js'), import('ol/source/ImageStatic.js'), import('ol/proj.js')]); scope.check();
      const tiff = await fromArrayBuffer(bytes); scope.add(() => { void tiff.close(); });
      const image = await tiff.getImage(); scope.check();
      const samples = image.getSamplesPerPixel(); validateRaster(image.getWidth(), image.getHeight(), samples);
      const keys = image.getGeoKeys();
      const code = Number(keys?.ProjectedCSTypeGeoKey || keys?.GeographicTypeGeoKey);
      if (![4326, 3857].includes(code)) throw new Error('GeoTIFF 首期需要明确 EPSG:4326 或 EPSG:3857；不推测缺失投影。');
      if (image.fileDirectory.hasTag('ModelTransformation')) throw new Error('首期不支持旋转/倾斜 GeoTIFF 网格，请先重投影为轴对齐栅格。');
      const extent = image.getBoundingBox();
      if (extent.length !== 4 || !extent.every(Number.isFinite) || extent[0]! >= extent[2]! || extent[1]! >= extent[3]!) throw new Error('GeoTIFF 地理范围无效。');
      const width = Math.min(image.getWidth(), 1024), height = Math.min(image.getHeight(), 1024);
      const values = await image.readRasters({ samples: [0], width, height, interleave: true, signal: scope.signal }); scope.check();
      let min = Infinity, max = -Infinity;
      const nodata = image.getGDALNoData();
      for (const v of values as any) if (Number.isFinite(v) && v !== nodata) { min = Math.min(min, v); max = Math.max(max, v); }
      if (!Number.isFinite(min)) throw new Error('GeoTIFF 第一波段没有有效数值。');
      const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
      const context = canvas.getContext('2d')!; const pixels = context.createImageData(width, height);
      for (let i = 0; i < width * height; i++) {
        const value = Number((values as any)[i]); const valid = Number.isFinite(value) && value !== nodata; const c = valid ? Math.round(255 * (value - min) / (max - min || 1)) : 0;
        pixels.data.set([c, Math.round(190 * (1 - c / 255)), 255 - c, valid ? 255 : 0], i * 4);
      }
      context.putImageData(pixels, 0, 0);
      const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob((result) => result ? resolve(result) : reject(new Error('栅格编码失败。')))); scope.check();
      const source = new ImageStatic({ url: scope.blob(blob), imageExtent: extent, projection: `EPSG:${code}` });
      map.addLayer(new ImageLayer({ source })); map.getView().fit(transformExtent(extent, `EPSG:${code}`, 'EPSG:3857'), { padding: [24, 24, 24, 24] });
      status.value = `波段 1 · 值域 ${min.toPrecision(5)} — ${max.toPrecision(5)} · EPSG:${code} · ${width}×${height} 有界预览`;
    }
  } catch (reason) { if (!scope.signal.aborted) { error.value = displayError(reason); scope.cleanup(); } }
});
</script>
<template><section class="domain-preview"><p class="notice">OpenLayers / geotiff.js · 本地只读；拖动平移，滚轮缩放。</p><p v-if="error" role="alert">{{ error }}</p><div v-show="!error" ref="target" class="surface"/><p class="notice">{{ status }}</p></section></template>
<style scoped>.domain-preview{height:100%;display:flex;flex-direction:column;min-height:360px}.surface{flex:1;min-height:320px;background:#e8edf0}.notice{font-size:12px;padding:8px;color:#667085}[role=alert]{padding:16px;color:#b42318}</style>
