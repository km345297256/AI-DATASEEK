<script setup lang="ts">
import { onMounted, ref } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { loadPluginBytes } from '../runtime';
import { safeName, validateRaster } from './guards';
import { displayError, useDomainScope } from './lifecycle';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const target = ref<HTMLDivElement>(); const error = ref(''); const busy = ref(true); const ready = ref(false);
const channels = ref<{ name: string; visible: boolean; color: [number, number, number] }[]>([]);
const t = ref(0), z = ref(0), maxT = ref(1), maxZ = ref(1), status = ref('读取单文件 OME-TIFF…');
const scope = useDomainScope(); let deck: any, source: any, ImageLayer: any, width = 0, height = 0; let cached: any[] = [], contrasts: number[][] = [];
const palette: [number, number, number][] = [[255, 60, 60], [50, 255, 90], [80, 110, 255], [255, 220, 40], [230, 50, 240], [40, 230, 240]];
function draw() {
  if (!deck || !cached.length) return;
  const selections = channels.value.map((_, c) => ({ c, t: t.value, z: z.value }));
  // The third-party layer sees only already validated bounded pixels, never URLs or file paths.
  const memorySource = { dtype: source.dtype, shape: source.shape, labels: source.labels, meta: source.meta, getRaster: async ({ selection, signal }: any) => { signal?.throwIfAborted(); scope.check(); return cached[selection.c]; } };
  deck.setProps({ layers: [new ImageLayer({ id: 'local-ome-tiff', loader: memorySource, selections, contrastLimits: contrasts, channelsVisible: channels.value.map((item) => item.visible), colors: channels.value.map((item) => item.color), pickable: false })] });
}
async function readSlice() {
  busy.value = true; error.value = '';
  try {
    if (!Number.isInteger(t.value) || t.value < 0 || t.value >= maxT.value || !Number.isInteger(z.value) || z.value < 0 || z.value >= maxZ.value) throw new Error('Z/T 切片索引超出图像范围。');
    // Release prior GPU textures before allocating replacement planes; sequential decoding bounds peak RAM.
    deck?.setProps({ layers: [] }); cached = []; contrasts = [];
    for (let c = 0; c < channels.value.length; c++) {
      scope.check(); const raster = await source.getRaster({ selection: { c, t: t.value, z: z.value }, signal: scope.signal }); scope.check();
      if (raster.width !== width || raster.height !== height || raster.data.length !== width * height) throw new Error('OME-TIFF 图像平面与元数据不一致；首期不支持交错 RGB 样本。');
      let min = Infinity, max = -Infinity;
      for (const value of raster.data) if (Number.isFinite(value)) { min = Math.min(min, value); max = Math.max(max, value); }
      if (!Number.isFinite(min)) { min = 0; max = 1; }
      contrasts.push([min, max > min ? max : min + 1]); cached.push(raster);
    }
    draw(); ready.value = true; status.value = `${width}×${height} · ${channels.value.length} 通道 · Z=${z.value} / T=${t.value}（0 起始） · 每通道按当前切片最小/最大值显示`;
  } catch (reason) { if (!scope.signal.aborted) error.value = displayError(reason); }
  finally { busy.value = false; }
}
onMounted(async () => {
  try {
    const bytes = await loadPluginBytes(props.file, props.plugin, scope.signal); scope.check();
    const [{ fromArrayBuffer }, { loadOmeTiff }, layers, { Deck, OrthographicView }] = await Promise.all([import('geotiff'), import('@vivjs/loaders'), import('@vivjs/layers'), import('@deck.gl/core')]); scope.check();
    const tiff = await fromArrayBuffer(bytes); scope.add(() => { void tiff.close(); });
    const first = await tiff.getImage(); scope.check();
    // TIFF ASCII values conventionally include a terminating NUL; XML parsers must not receive it.
    const description = String(await first.fileDirectory.loadValue('ImageDescription') || '').replace(/\0+$/, ''); scope.check();
    if (!/<OME(?:\s|>)/.test(description) || description.length > 1024 * 1024 || /<!DOCTYPE|<!ENTITY|<BinaryOnly|<UUID[^>]*FileName/i.test(description)) throw new Error('需要单文件 OME-TIFF 元数据；外部 OME、XML 实体和关联图像文件不开放。');
    const xml = new DOMParser().parseFromString(description, 'application/xml'); const pixels = xml.getElementsByTagName('Pixels')[0];
    if (!pixels || xml.querySelector('parsererror')) throw new Error('OME-TIFF 元数据不可解析。');
    width = Number(pixels.getAttribute('SizeX')); height = Number(pixels.getAttribute('SizeY'));
    const channelCount = Number(pixels.getAttribute('SizeC')); maxT.value = Number(pixels.getAttribute('SizeT')); maxZ.value = Number(pixels.getAttribute('SizeZ'));
    validateRaster(width, height, channelCount);
    if (width !== first.getWidth() || height !== first.getHeight() || first.getSamplesPerPixel() !== 1 || channelCount > 6 || ![maxT.value, maxZ.value].every((n) => Number.isSafeInteger(n) && n > 0) || channelCount * maxT.value * maxZ.value > 1024) throw new Error('首期最多 6 个独立灰度通道、1,024 个图像平面，不支持交错 RGB。');
    // No decoder pool: bounded local chunks use the library's in-realm decoder, no stranded workers.
    // Viv bundles geotiff 2 internally; never pass a geotiff 3 Pool across this version boundary.
    const loaded = await loadOmeTiff(new File([bytes], 'local.ome.tif')); scope.check();
    source = loaded.data[0]; if (!source) throw new Error('OME-TIFF 中没有可读图像。');
    channels.value = Array.from({ length: channelCount }, (_, c) => ({ name: safeName(loaded.metadata.Pixels.Channels[c]?.Name) || `通道 ${c + 1}`, visible: true, color: palette[c]! }));
    ImageLayer = layers.ImageLayer;
    const canvas = document.createElement('canvas'); target.value!.append(canvas);
    deck = new Deck({ canvas, parent: target.value, views: new OrthographicView({ id: 'image', flipY: true }), initialViewState: { target: [width / 2, height / 2, 0], zoom: Math.log2(Math.min((target.value!.clientWidth || 400) / width, (target.value!.clientHeight || 350) / height)) }, controller: true, layers: [] });
    scope.add(() => { deck?.finalize(); deck = undefined; cached = []; source = undefined; });
    const observer = new ResizeObserver(() => deck?.setProps({ width: target.value!.clientWidth, height: target.value!.clientHeight })); observer.observe(target.value!); scope.add(() => observer.disconnect());
    await readSlice();
  } catch (reason) { if (!scope.signal.aborted) { error.value = displayError(reason); scope.cleanup(); } busy.value = false; }
});
</script>
<template><section class="domain-preview"><p>Viv · 本地单文件 OME-TIFF 多通道叠加。原始数值不修改，显示颜色不代表样品固有颜色。</p><div v-if="ready" class="controls"><label v-for="(channel,index) in channels" :key="index"><input v-model="channel.visible" type="checkbox" :disabled="busy" @change="draw"/>{{ channel.name }}</label><label>Z <input v-model.number="z" type="number" :min="0" :max="maxZ-1" :disabled="busy"/></label><label>T <input v-model.number="t" type="number" :min="0" :max="maxT-1" :disabled="busy"/></label><button :disabled="busy" @click="readSlice">应用切片</button></div><p v-if="error" role="alert">{{ error }}</p><div ref="target" class="surface"/><p>{{ busy ? '有界读取中…' : status }}</p></section></template>
<style scoped>.domain-preview{height:100%;display:flex;flex-direction:column;min-height:420px}.surface{position:relative;flex:1;min-height:320px;background:#101820}.controls{display:flex;flex-wrap:wrap;gap:8px;padding:8px;font-size:12px}input[type=number]{width:55px;border:1px solid #aaa}button{border:1px solid #aaa;padding:2px 8px}p{font-size:12px;padding:8px;color:#667085}[role=alert]{color:#b42318}</style>
