<script setup lang="ts">
import { onMounted, ref } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { loadPluginBytes } from '../runtime';
import { boundedCzml, boundedGeoJSON } from './guards';
import { displayError, useDomainScope } from './lifecycle';
import { isolatedDomainFrame } from './frame';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const target = ref<HTMLElement>(); const error = ref(''); const ready = ref(false); const scope = useDomainScope();
onMounted(async () => {
  try {
    const bytes = await loadPluginBytes(props.file, props.plugin, scope.signal); scope.check();
    const czml = /\.czml$/i.test(props.file.filename); const data = czml ? boundedCzml(bytes) : boundedGeoJSON(bytes);
    // Cesium's geometry TaskProcessors are global to its JS realm, not disposed by Viewer.destroy().
    // A dedicated local frame owns that pool without touching other live plugin viewers.
    const timer = window.setTimeout(() => { error.value = 'Cesium 本地图层初始化超时，已释放预览。'; scope.cleanup(); }, 45000);
    scope.add(() => clearTimeout(timer));
    const child = isolatedDomainFrame(target.value!, scope, {
      title: 'Cesium 本地地球与轨迹预览',
      allowSdkEval: true,
      head: '<link rel="stylesheet" href="__ASSET_ROOT__cesium/Widgets/widgets.css"><script nonce="__NONCE__">window.CESIUM_BASE_URL="__ASSET_ROOT__cesium/";<\/script><script src="__ASSET_ROOT__cesium/Cesium.js"><\/script>',
      bootstrap: `let viewer,loaded=false;
window.addEventListener('pagehide',()=>{if(viewer&&!viewer.isDestroyed())viewer.destroy();});
window.addEventListener('message',async(event)=>{
 if(!trusted(event)||event.data.type!=='load'||loaded)return;loaded=true;
 try{
  const {data,czml}=event.data.payload,C=window.Cesium;
  viewer=new C.Viewer(document.getElementById('viewer'),{baseLayer:false,baseLayerPicker:false,geocoder:false,homeButton:true,sceneModePicker:true,navigationHelpButton:false,animation:czml,timeline:czml,fullscreenButton:false,infoBox:false,selectionIndicator:false,skyBox:false,skyAtmosphere:false,terrainProvider:new C.EllipsoidTerrainProvider(),requestRenderMode:false});
  viewer.scene.globe.baseColor=C.Color.fromCssColorString('#203345');viewer.scene.globe.enableLighting=false;
  const source=czml?await C.CzmlDataSource.load(data):await C.GeoJsonDataSource.load(data,{clampToGround:false,markerColor:C.Color.ORANGE,stroke:C.Color.CYAN,fill:C.Color.CYAN.withAlpha(0.25)});
  await viewer.dataSources.add(source);await viewer.zoomTo(source);viewer.scene.requestRenderMode=true;viewer.scene.requestRender();notify('ready');
 }catch{notify('error');}
});notify('boot');`,
      onMessage: (type) => {
        if (type === 'boot') child.post('load', { data, czml });
        else if (type === 'ready') { clearTimeout(timer); ready.value = true; }
        else if (type === 'error') { error.value = 'Cesium 无法显示本地图层，或当前设备不支持 WebGL。'; scope.cleanup(); }
      },
    });
  } catch (reason) { if (!scope.signal.aborted) { error.value = displayError(reason); scope.cleanup(); } }
});
</script>
<template><section class="domain-preview"><p>CesiumJS · 本地 GeoJSON / 位置轨迹 CZML · WGS84 椭球；无在线地形、卫星底图和外部模型 <span v-if="ready">· 本地图层已加载</span></p><div v-if="error" role="alert">{{ error }}</div><div v-show="!error" ref="target" class="surface"/></section></template>
<style scoped>.domain-preview{height:100%;display:flex;flex-direction:column;min-height:360px}.surface{position:relative;flex:1;min-height:320px}p{font-size:12px;padding:8px;color:#667085}[role=alert]{padding:16px;color:#b42318}</style>
