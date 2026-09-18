<script setup lang="ts">
import { onMounted, ref } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { loadPluginBytes } from '../runtime';
import { boundedCzml, boundedGeoJSON } from './guards';
import { displayError, useDomainScope } from './lifecycle';
import { isolatedDomainFrame } from './frame';
import { cesiumBootstrap, isCesiumBasemap, type CesiumBasemap } from './cesiumBootstrap';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const target = ref<HTMLElement>(); const error = ref(''); const ready = ref(false); const scope = useDomainScope();
const basemap = ref<CesiumBasemap>('natural-earth'); const basemapStatus = ref('loading'); const basemapFallback = ref(false);
let child: ReturnType<typeof isolatedDomainFrame> | undefined;
const changeBasemap = () => { if (isCesiumBasemap(basemap.value)) child?.post('basemap', { mode: basemap.value }); };
onMounted(async () => {
  try {
    const bytes = await loadPluginBytes(props.file, props.plugin, scope.signal); scope.check();
    const czml = /\.czml$/i.test(props.file.filename); const data = czml ? boundedCzml(bytes) : boundedGeoJSON(bytes);
    // Cesium's geometry TaskProcessors are global to its JS realm, not disposed by Viewer.destroy().
    // A dedicated local frame owns that pool without touching other live plugin viewers.
    const timer = window.setTimeout(() => { error.value = 'Cesium 本地图层初始化超时，已释放预览。'; scope.cleanup(); }, 45000);
    scope.add(() => clearTimeout(timer));
    child = isolatedDomainFrame(target.value!, scope, {
      title: 'Cesium 本地地球与轨迹预览',
      allowSdkEval: true,
      head: '<link rel="stylesheet" href="__ASSET_ROOT__cesium/Widgets/widgets.css"><script nonce="__NONCE__">window.CESIUM_BASE_URL="__ASSET_ROOT__cesium/";<\/script><script src="__ASSET_ROOT__cesium/Cesium.js"><\/script>',
      bootstrap: cesiumBootstrap,
      onMessage: (type, payload) => {
        if (type === 'boot') child?.post('load', { data, czml });
        else if (type === 'ready') { clearTimeout(timer); ready.value = true; }
        else if (type === 'basemap' && isCesiumBasemap(payload?.mode) && ['loading', 'ready'].includes(payload?.status)) {
          basemap.value = payload.mode; basemapStatus.value = payload.status; basemapFallback.value = payload.fallback === true;
        }
        else if (type === 'error') { error.value = 'Cesium 无法显示本地图层，或当前设备不支持 WebGL。'; scope.cleanup(); }
      },
    });
  } catch (reason) { if (!scope.signal.aborted) { error.value = displayError(reason); scope.cleanup(); } }
});
</script>
<template>
  <section class="domain-preview" :data-basemap="basemap" :data-basemap-status="basemapStatus">
    <p>CesiumJS · 本地 GeoJSON / 位置轨迹 CZML · WGS84 椭球；无在线地形和外部模型 <span v-if="ready">· 本地图层已加载</span></p>
    <div class="basemap-controls">
      <label>底图 <select v-model="basemap" aria-label="底图" data-testid="cesium-basemap-select" :disabled="!ready || !!error" @change="changeBasemap">
        <option value="natural-earth">内置自然地理</option><option value="grid">参考网格</option><option value="none">无底图</option>
      </select></label>
      <span v-if="basemapStatus === 'loading'" role="status">正在加载本地底图…</span>
      <span class="basemap-note">Natural Earth II · 公共领域 · 离线全球概览，不含街道路网；参考网格不代表固定经纬度间隔。</span>
    </div>
    <p v-if="basemapFallback" role="status" data-testid="cesium-basemap-warning" class="basemap-warning">本地底图暂不可用，已切换为{{ basemap === 'grid' ? '参考网格' : '无底图' }}；数据图层和当前视角已保留。</p>
    <div v-if="error" role="alert">{{ error }}</div><div v-show="!error" ref="target" class="surface"/>
  </section>
</template>
<style scoped>.domain-preview{height:100%;display:flex;flex-direction:column;min-height:360px}.surface{position:relative;flex:1;min-height:320px}p{font-size:12px;padding:8px;color:#667085}.basemap-controls{display:flex;align-items:center;gap:8px 16px;flex-wrap:wrap;padding:0 8px 8px;font-size:12px;color:#667085}.basemap-controls label{display:flex;align-items:center;gap:8px;color:#344054}.basemap-controls select{padding:5px 8px;border:1px solid #d0d5dd;border-radius:6px;background:#fff;color:#344054}.basemap-note{flex:1;min-width:200px}.basemap-warning{padding-top:0;color:#946200}[role=alert]{padding:16px;color:#b42318}</style>
