<script setup lang="ts">
import { onMounted, ref } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { loadPluginBytes } from '../runtime';
import { boundedGeoJSON, geoCenter } from './guards';
import { displayError, useDomainScope } from './lifecycle';
import { isolatedDomainFrame } from './frame';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const target = ref<HTMLElement>(); const error = ref(''); const count = ref(0); const scope = useDomainScope();
onMounted(async () => {
  try {
    const bytes = await loadPluginBytes(props.file, props.plugin, scope.signal); const data = boundedGeoJSON(bytes); scope.check();
    const [{ Deck }, { GeoJsonLayer }] = await Promise.all([import('@deck.gl/core'), import('@deck.gl/layers')]); scope.check();
    count.value = data.features.length;
    const mapElement = document.createElement('div'), canvas = document.createElement('canvas');
    mapElement.style.cssText = 'position:absolute;inset:0'; canvas.style.cssText = 'position:absolute;inset:0;pointer-events:none';
    target.value!.append(mapElement, canvas);
    const center = geoCenter(data);
    const deck = new Deck({ canvas, controller: false, initialViewState: { longitude: center[0], latitude: center[1], zoom: 3 }, layers: [new GeoJsonLayer({ id: 'local-geojson', data, stroked: true, filled: true, getFillColor: [35, 170, 190, 100], getLineColor: [255, 170, 55, 255], getPointRadius: 5, pointRadiusUnits: 'pixels', lineWidthMinPixels: 2, pickable: false })] });
    scope.add(() => deck.finalize());
    const timer = window.setTimeout(() => { error.value = 'MapLibre 本地地图初始化超时，已释放预览。'; scope.cleanup(); }, 30000); scope.add(() => clearTimeout(timer));
    // MapLibre 6 retains a global RTL dispatcher worker even after Map.remove().
    // Isolate the map realm; deck remains a disposable parent GPU overlay.
    const child = isolatedDomainFrame(mapElement, scope, {
      title: 'MapLibre 本地离线地图',
      head: '<link rel="stylesheet" href="__ASSET_ROOT__maplibre/maplibre-gl.css">',
      bootstrap: `const {Map,setWorkerUrl}=await import(assetRoot+'maplibre/maplibre-gl.mjs');
setWorkerUrl(assetRoot+'maplibre/maplibre-gl-worker.mjs');let map,loaded=false;
window.addEventListener('pagehide',()=>map?.remove());
window.addEventListener('message',(event)=>{
 if(!trusted(event)||event.data.type!=='load'||loaded)return;loaded=true;
 try{
  map=new Map({container:'viewer',style:{version:8,sources:{},layers:[{id:'offline-background',type:'background',paint:{'background-color':'#102030'}}]},center:event.data.payload.center,zoom:3,attributionControl:false,localIdeographFontFamily:false});
  const sync=()=>{const c=map.getCenter();notify('camera',{longitude:c.lng,latitude:c.lat,zoom:map.getZoom(),bearing:map.getBearing(),pitch:map.getPitch()});};
  map.on('move',sync);map.on('load',()=>{sync();notify('ready');});window.addEventListener('resize',()=>{map.resize();sync();});
 }catch{notify('error');}
});notify('boot');`,
      onMessage: (type, payload) => {
        if (type === 'boot') child.post('load', { center });
        else if (type === 'ready') clearTimeout(timer);
        else if (type === 'camera' && ['longitude', 'latitude', 'zoom', 'bearing', 'pitch'].every((key) => Number.isFinite(payload?.[key]))) deck.setProps({ viewState: payload });
        else if (type === 'error') { error.value = 'MapLibre 无法初始化本地地图。'; scope.cleanup(); }
      },
    });
    const observer = new ResizeObserver(() => deck.setProps({ width: target.value!.clientWidth, height: target.value!.clientHeight })); observer.observe(target.value!); scope.add(() => observer.disconnect());
  } catch (reason) { if (!scope.signal.aborted) { error.value = displayError(reason); scope.cleanup(); } }
});
</script>
<template><section class="domain-preview"><p>MapLibre + deck.gl · {{ count }} 个本地要素 · 空白底图，不访问外部瓦片、字体或云服务</p><div v-if="error" role="alert">{{ error }}</div><div v-show="!error" ref="target" class="surface"/></section></template>
<style scoped>.domain-preview{height:100%;display:flex;flex-direction:column;min-height:360px}.surface{position:relative;flex:1;min-height:320px}p{font-size:12px;padding:8px;color:#667085}[role=alert]{padding:16px;color:#b42318}</style>
