<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { loadPluginBytes } from '../runtime';
import { boundedGeoJSON, validateRaster } from './guards';
import { displayError, useDomainScope } from './lifecycle';
import 'ol/ol.css';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const target = ref<HTMLElement>(); const error = ref(''); const status = ref('读取本地数据…');
const scope = useDomainScope();
const band = ref(1), bandCount = ref(0), busy = ref(true), colorRange = ref<{ min: number; max: number }>();
const nodataLabel = ref(''), invalidCount = ref(0);
const colorStyle = computed(() => ({ background: colorRange.value?.min === colorRange.value?.max ? 'rgb(0,190,255)' : 'linear-gradient(90deg, rgb(0,190,255), rgb(255,0,0))' }));
let selectBand: ((value: number) => Promise<void>) | undefined;
watch(band, value => { if (selectBand) void selectBand(value); });
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
      const tiff = await fromArrayBuffer(bytes); scope.add(() => { void Promise.resolve(tiff.close()).catch(() => { /* close after cancelled decoding */ }); }); scope.check();
      const image = await tiff.getImage(); scope.check();
      const samples = image.getSamplesPerPixel(); validateRaster(image.getWidth(), image.getHeight(), samples);
      if (samples > 256) throw new Error('GeoTIFF 交互式波段选择最多支持 256 个波段。');
      const keys = image.getGeoKeys();
      const code = Number(keys?.ProjectedCSTypeGeoKey || keys?.GeographicTypeGeoKey);
      if (![4326, 3857].includes(code)) throw new Error('GeoTIFF 首期需要明确 EPSG:4326 或 EPSG:3857；不推测缺失投影。');
      if (image.fileDirectory.hasTag('ModelTransformation')) throw new Error('首期不支持旋转/倾斜 GeoTIFF 网格，请先重投影为轴对齐栅格。');
      const extent = image.getBoundingBox();
      if (extent.length !== 4 || !extent.every(Number.isFinite) || extent[0]! >= extent[2]! || extent[1]! >= extent[3]!) throw new Error('GeoTIFF 地理范围无效。');
      const width = Math.min(image.getWidth(), 1024), height = Math.min(image.getHeight(), 1024);
      const nodata = image.getGDALNoData();
      bandCount.value = samples;
      nodataLabel.value = nodata == null ? '未声明 NoData；非有限值仍透明显示' : `NoData = ${String(nodata)}；NoData 与非有限值透明显示`;
      let generation = 0, pending: AbortController | undefined, fitted = false;
      let releaseVisible: (() => void) | undefined;
      scope.add(() => { generation++; pending?.abort(); releaseVisible?.(); releaseVisible = undefined; selectBand = undefined; });
      selectBand = async (selected: number) => {
        const serial = ++generation;
        pending?.abort(); pending = new AbortController();
        const controller = pending;
        const current = () => !scope.signal.aborted && !controller.signal.aborted && serial === generation;
        const check = () => { if (!current()) throw new DOMException('Raster request superseded', 'AbortError'); };
        busy.value = true; error.value = ''; colorRange.value = undefined; invalidCount.value = 0;
        status.value = `正在读取波段 ${selected}…`;
        releaseVisible?.(); releaseVisible = undefined;
        let canvas: HTMLCanvasElement | undefined, url: string | undefined;
        let candidateLayer: InstanceType<typeof ImageLayer> | undefined;
        let candidateSource: InstanceType<typeof ImageStatic> | undefined;
        let committed = false;
        const clearCanvas = () => { if (canvas) { canvas.width = 0; canvas.height = 0; } };
        const release = () => {
          if (candidateLayer) { map.removeLayer(candidateLayer); candidateLayer.setSource(null); candidateLayer.dispose(); candidateLayer = undefined; }
          candidateSource?.dispose(); candidateSource = undefined;
          if (url) URL.revokeObjectURL(url); url = undefined; clearCanvas();
        };
        controller.signal.addEventListener('abort', clearCanvas, { once: true });
        try {
          check();
          if (!Number.isSafeInteger(selected) || selected < 1 || selected > samples) throw new Error('GeoTIFF 波段编号无效。');
          // TIFF bytes/image metadata are reused. No repeat download or growing band cache.
          const values = await image.readRasters({ samples: [selected - 1], width, height, interleave: true, resampleMethod: 'nearest', signal: controller.signal }) as ArrayLike<number>;
          check();
          if (values.length !== width * height) throw new Error('GeoTIFF 波段数据长度不符合预览尺寸。');
          let min = Infinity, max = -Infinity, invalid = 0;
          for (let i = 0; i < values.length; i++) {
            const value = values[i]!;
            if (typeof value !== 'number') throw new Error('GeoTIFF 波段数值类型不支持。');
            if (Number.isFinite(value) && value !== nodata) { min = Math.min(min, value); max = Math.max(max, value); }
            else invalid++;
          }
          if (!Number.isFinite(min)) throw new Error(`GeoTIFF 波段 ${selected} 没有有效数值。`);
          canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
          const context = canvas.getContext('2d');
          if (!context) throw new Error('浏览器无法创建栅格画布。');
          const pixels = context.createImageData(width, height);
          // Scale before subtraction, so extreme but finite Float64 ranges cannot overflow.
          const extentScale = Math.max(Math.abs(min), Math.abs(max), 1), span = max / extentScale - min / extentScale;
          for (let i = 0; i < values.length; i++) {
            const value = values[i]!, valid = Number.isFinite(value) && value !== nodata;
            const normalized = valid && span ? (value / extentScale - min / extentScale) / span : 0;
            const c = Math.max(0, Math.min(255, Math.round(255 * normalized)));
            pixels.data.set([c, Math.round(190 * (1 - c / 255)), 255 - c, valid ? 255 : 0], i * 4);
          }
          context.putImageData(pixels, 0, 0);
          const blob = await new Promise<Blob>((resolve, reject) => canvas!.toBlob(result => result ? resolve(result) : reject(new Error('栅格编码失败。'))));
          check(); clearCanvas();
          url = URL.createObjectURL(blob);
          candidateSource = new ImageStatic({ url, imageExtent: extent, projection: `EPSG:${code}`, interpolate: false });
          candidateLayer = new ImageLayer({ source: candidateSource });
          map.addLayer(candidateLayer); releaseVisible = release; committed = true;
          if (!fitted) { map.getView().fit(transformExtent(extent, `EPSG:${code}`, 'EPSG:3857'), { padding: [24, 24, 24, 24] }); fitted = true; }
          colorRange.value = { min, max }; invalidCount.value = invalid;
          status.value = `波段 ${selected} / ${samples} · EPSG:${code} · ${width}×${height} 最近邻有界预览 · 色标为预览范围，非全量统计`;
        } catch (reason) { if (current()) { error.value = displayError(reason); status.value = '请选择其他波段，或下载原件检查。'; } }
        finally {
          controller.signal.removeEventListener('abort', clearCanvas);
          if (!committed) release();
          if (current()) busy.value = false;
        }
      };
      await selectBand(1);
    }
  } catch (reason) { if (!scope.signal.aborted) { error.value = displayError(reason); scope.cleanup(); } }
  finally { if (!scope.signal.aborted && !selectBand) busy.value = false; }
});
</script>
<template><section class="domain-preview"><p class="notice">OpenLayers / geotiff.js · 本地只读；拖动平移，滚轮缩放。</p>
  <div v-if="bandCount" class="raster-controls"><label>波段 <select v-model.number="band" :disabled="busy"><option v-for="value in bandCount" :key="value" :value="value">{{ value }}</option></select></label><span v-if="busy" role="status">读取中…</span></div>
  <div v-if="colorRange" class="legend" aria-label="当前波段色标"><span>{{ colorRange.min.toPrecision(5) }}</span><span class="color-ramp" :style="colorStyle"/><span>{{ colorRange.max.toPrecision(5) }}</span><span v-if="colorRange.min === colorRange.max">常量波段</span></div>
  <p v-if="bandCount" class="notice">{{ nodataLabel }} · 本次预览 {{ invalidCount }} 个无效像素。显示原始波段值，不应用额外比例或偏移。</p>
  <p v-if="error" role="alert">{{ error }}</p><div v-show="!error" ref="target" class="surface" :aria-busy="busy"/><p class="notice">{{ status }}</p></section></template>
<style scoped>.domain-preview{height:100%;display:flex;flex-direction:column;min-height:360px}.surface{flex:1;min-height:320px;background:#e8edf0}.notice{font-size:12px;padding:8px;color:#667085}.raster-controls,.legend{display:flex;align-items:center;gap:12px;padding:4px 8px;font-size:12px}.raster-controls select{border:1px solid #d0d5dd;border-radius:4px;padding:3px 8px}.color-ramp{width:140px;height:12px;border:1px solid #d0d5dd}[role=alert]{padding:16px;color:#b42318}</style>
