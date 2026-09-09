<script setup lang="ts">
import { onMounted, ref } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { loadPluginBytes } from '../runtime';
import { fitsWcsHeader } from './guards';
import { displayError, useDomainScope } from './lifecycle';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const target = ref<HTMLElement>(); const error = ref(''); const status = ref('读取本地 FITS/WCS…'); const scope = useDomainScope();
onMounted(async () => {
  try {
    const bytes = await loadPluginBytes(props.file, props.plugin, scope.signal); const header = fitsWcsHeader(bytes); scope.check();
    const nonce = crypto.randomUUID().replace(/-/g, '');
    // Aladin owns an animation loop and WASM context without a public dispose API.
    // A dedicated frame gives deterministic lifetime and its CSP blocks automatic survey/catalog traffic.
    const frame = document.createElement('iframe'); frame.title = 'Aladin Lite 本地 FITS 天球预览'; frame.setAttribute('sandbox', 'allow-scripts allow-same-origin'); frame.style.cssText = 'border:0;width:100%;height:100%;min-height:350px';
    const asset = new URL(`${import.meta.env.BASE_URL}visualization-assets/aladin/aladin.js`, location.origin).href;
    frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-${nonce}' 'self' 'wasm-unsafe-eval'; style-src 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' data: blob:; worker-src 'self' blob:; font-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none';"><style>html,body,#sky{height:100%;width:100%;margin:0;background:#101820;overflow:hidden}</style></head><body><div id="sky"></div><script type="module" nonce="${nonce}">
import A from '${asset}';
const channel='${nonce}',parentOrigin=${JSON.stringify(location.origin)}; let loaded=false;
const notify=(type)=>parent.postMessage({channel,type},parentOrigin);
window.addEventListener('message',async(event)=>{
 if(event.source!==parent||event.origin!==parentOrigin||event.data?.channel!==channel||loaded)return;
 loaded=true;
 try{
  await A.init;
  const viewer=A.aladin(document.getElementById('sky'),{survey:[],target:event.data.ra+' '+event.data.dec,fov:1,mode:'dark',cooFrame:'ICRSd',showLayersControl:false,showSimbadPointerControl:false,showShareControl:false,showSettingsControl:false,showContextMenu:false,showCooGridControl:true,showCooGrid:true,showCatalog:false,log:false});
  const url=URL.createObjectURL(new Blob([event.data.bytes],{type:'application/fits'}));
  const image=A.image(url,{name:'Local FITS',successCallback:(ra,dec,fov)=>{viewer.gotoRaDec(ra,dec);if(Number.isFinite(fov)&&fov>0)viewer.setFoV(Math.min(180,fov*1.1));URL.revokeObjectURL(url);notify('ready');},errorCallback:()=>notify('error')});
  viewer.setOverlayImageLayer(image,'local-fits');
 }catch{notify('error');}
});
notify('boot');
<\/script></body></html>`;
    scope.add(() => frame.remove());
    const handler = (event: MessageEvent) => {
      if (event.source !== frame.contentWindow || event.origin !== location.origin || event.data?.channel !== nonce) return;
      if (event.data.type === 'boot') frame.contentWindow!.postMessage({ channel: nonce, bytes, ra: header.ra, dec: header.dec }, location.origin, [bytes]);
      else if (event.data.type === 'ready') { status.value = '本地 FITS 天球 WCS · 网格为赤道坐标；没有在线巡天图层'; clearTimeout(timer); }
      else if (event.data.type === 'error') { error.value = 'Aladin 无法显示该 FITS/WCS 或当前设备不支持 WebGL2；可改用普通 FITS 图像插件。'; scope.cleanup(); }
    };
    window.addEventListener('message', handler); scope.add(() => window.removeEventListener('message', handler));
    const timer = window.setTimeout(() => { error.value = 'Aladin 初始化或 FITS 读取超时，已释放预览。'; scope.cleanup(); }, 30000); scope.add(() => clearTimeout(timer));
    target.value!.append(frame);
  } catch (reason) { if (!scope.signal.aborted) { error.value = displayError(reason); scope.cleanup(); } }
});
</script>
<template><section class="domain-preview"><p>{{ status }}</p><p v-if="error" role="alert">{{ error }}</p><div ref="target" class="surface"/></section></template>
<style scoped>.domain-preview{height:100%;display:flex;flex-direction:column;min-height:400px}.surface{flex:1;min-height:350px}p{font-size:12px;padding:8px;color:#667085}[role=alert]{color:#b42318}</style>
