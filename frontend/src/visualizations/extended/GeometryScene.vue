<template>
  <div><p class="py-2 text-xs text-gray-600">拖动旋转、滚轮缩放；三轴等比例。仅改变显示原点与统一比例，原始坐标保持不变。</p>
    <p v-if="range" data-testid="geometry-range" class="text-sm">所选窗口颜色范围：{{ range[0] }} — {{ range[1] }}；无效值为灰色。</p>
    <p v-if="error" role="alert" class="text-amber-700">{{ error }}</p><div ref="target" data-testid="geometry-scene" class="h-[420px] w-full min-w-0 overflow-hidden rounded border" aria-label="交互式三维几何" />
  </div>
</template>
<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { localScene, type SceneGeometry } from './geometryData';
const props = defineProps<{scene:SceneGeometry}>(), scope = usePreviewLoad(), target = ref<HTMLDivElement>(), error = ref('');
const range = computed(() => { const v = props.scene.values?.filter((x): x is number => x !== null) ?? []; return v.length ? [Math.min(...v),Math.max(...v)] : null; });
async function draw() {
  const load = scope.begin(); error.value = ''; if (!target.value) return;
  try {
    const [T,{OrbitControls}] = await Promise.all([import('three'),import('three/examples/jsm/controls/OrbitControls.js')]); load.assertCurrent();
    const host = target.value, source = props.scene, local = localScene(source), scene = new T.Scene(); scene.background = new T.Color('#f7fafc');
    const renderer = new T.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
    const camera = new T.PerspectiveCamera(40,1,0.001,100); const disposables: {dispose:()=>void}[] = [];
    load.onDispose(()=>{disposables.forEach(x=>x.dispose());renderer.dispose();renderer.forceContextLoss();renderer.domElement.remove();});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1,2)); host.replaceChildren(renderer.domElement);
    const geometry = new T.BufferGeometry(); disposables.push(geometry); geometry.setAttribute('position',new T.BufferAttribute(local.positions,3)); geometry.computeBoundingBox();
    const center = geometry.boundingBox!.getCenter(new T.Vector3()), extent = geometry.boundingBox!.getSize(new T.Vector3()).length() || 1;
    camera.position.copy(center).add(new T.Vector3(1,0.8,1.4).normalize().multiplyScalar(extent*2.2));
    const controls = new OrbitControls(camera,renderer.domElement); disposables.push(controls); controls.target.copy(center); controls.update();
    if (source.edges.length) { const line = new T.BufferGeometry(); disposables.push(line); line.setAttribute('position',new T.BufferAttribute(local.positions,3));line.setIndex(source.edges.flat()); const material = new T.LineBasicMaterial({color:'#526b7c',transparent:true,opacity:0.55}); disposables.push(material); scene.add(new T.LineSegments(line,material)); }
    const markers = new T.BufferGeometry(); disposables.push(markers); markers.setAttribute('position',new T.BufferAttribute(local.markers,3));
    const color = new T.Color(), limits = range.value; const colors = source.markers.flatMap((_,i)=>{
      const value=source.values?.[i]; if (value === null) color.set('#a0a0a0'); else if (value !== undefined && limits) color.setHSL((1-(limits[1]===limits[0]?0.5:(value-limits[0]!)/(limits[1]!-limits[0]!)))*0.66,0.8,0.45); else color.set('#19896c'); return [color.r,color.g,color.b];
    }); markers.setAttribute('color',new T.Float32BufferAttribute(colors,3)); const material = new T.PointsMaterial({size:source.association==='atoms'?7:5,sizeAttenuation:false,vertexColors:true}); disposables.push(material);scene.add(new T.Points(markers,material));
    function render() { if (load.isCurrent()) renderer.render(scene,camera); }
    function resize() { if (!load.isCurrent()) return; const width = Math.max(1,host.clientWidth), height = Math.max(1,host.clientHeight); renderer.setSize(width,height); camera.aspect=width/height;camera.updateProjectionMatrix();render(); }
    controls.addEventListener('change',render);load.onDispose(()=>controls.removeEventListener('change',render));
    const observer = new ResizeObserver(resize);observer.observe(host);load.onDispose(()=>observer.disconnect());resize();
    renderer.domElement.dataset.geometryPoints=String(source.markers.length);renderer.domElement.dataset.geometryEdges=String(source.edges.length);renderer.domElement.dataset.geometryAssociation=source.association;
  } catch { if (load.isCurrent()) { scope.begin(); error.value='三维绘图区无法初始化，请检查浏览器 WebGL 支持。'; } }
}
watch([target,()=>props.scene],()=>void draw(),{flush:'post'});onScopeDispose(()=>{error.value='';});
</script>
