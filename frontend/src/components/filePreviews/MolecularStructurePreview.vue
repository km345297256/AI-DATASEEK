<template>
  <div class="flex min-h-0 flex-1 flex-col bg-white">
    <div class="flex flex-wrap items-center gap-2 border-b border-[var(--border-main)] px-3 py-2 text-xs">
      <label class="flex items-center gap-1"><span>样式</span><select v-model="styleMode" class="rounded border px-1 py-1"><option value="ball-stick">球棍</option><option value="stick">棍状</option><option value="line">线框</option><option value="sphere">空间填充</option><option v-if="isProtein" value="cartoon">卡通</option></select></label>
      <label class="flex items-center gap-1"><input v-model="showHydrogen" type="checkbox" />氢原子</label>
      <label v-if="prepared?.supports_unit_cell" class="flex items-center gap-1"><input v-model="showUnitCell" type="checkbox" />晶胞</label>
      <button type="button" class="rounded border px-2 py-1 hover:bg-gray-100" @click="resetView">复位</button>
      <button type="button" class="rounded border px-2 py-1 hover:bg-gray-100" @click="exportPng">导出 PNG</button>
      <span class="ml-auto text-[var(--text-tertiary)]">点击两个原子测距</span>
    </div>
    <div class="flex h-[calc(100vh-132px)] min-h-[420px] min-w-0 flex-1 bg-white">
      <aside class="flex w-[42%] min-w-[300px] max-w-[520px] flex-none flex-col border-r border-[var(--border-main)] bg-white p-4 text-sm">
        <div class="flex items-end justify-between gap-3">
          <div><div class="text-xs text-[var(--text-tertiary)]">分子组成</div><div class="mt-1 break-words text-xl font-semibold text-[var(--text-primary)]">{{ formulaText || '—' }}</div></div>
          <span class="text-[11px] text-[var(--text-tertiary)]">二维结构式</span>
        </div>
        <div class="mt-3 min-h-[260px] flex-1 rounded-lg border border-[var(--border-main)] bg-[#fbfcfe]">
          <svg v-if="structure2dAtoms.length" class="h-full min-h-[260px] w-full" viewBox="0 0 520 520" role="img" aria-label="二维分子结构式">
            <g stroke="#1f2937" stroke-linecap="round">
              <template v-for="bond in structure2dBonds" :key="bond.key">
                <line :x1="bond.x1" :y1="bond.y1" :x2="bond.x2" :y2="bond.y2" stroke-width="4" />
                <line v-if="bond.order >= 2" :x1="bond.x1 + bond.offsetX" :y1="bond.y1 + bond.offsetY" :x2="bond.x2 + bond.offsetX" :y2="bond.y2 + bond.offsetY" stroke-width="3" />
                <line v-if="bond.order >= 3" :x1="bond.x1 - bond.offsetX" :y1="bond.y1 - bond.offsetY" :x2="bond.x2 - bond.offsetX" :y2="bond.y2 - bond.offsetY" stroke-width="3" />
              </template>
            </g>
            <g v-for="atom in structure2dAtoms" :key="atom.key">
              <circle v-if="atom.showLabel" :cx="atom.x" :cy="atom.y" r="17" fill="#fbfcfe" />
              <text v-if="atom.showLabel" :x="atom.x" :y="atom.y + 8" text-anchor="middle" font-size="24" font-weight="600" :fill="atom.color">{{ atom.elem }}</text>
              <circle v-else :cx="atom.x" :cy="atom.y" r="3.5" fill="#111827" />
            </g>
          </svg>
          <div v-else class="flex h-full min-h-[260px] items-center justify-center text-xs text-[var(--text-tertiary)]">暂无可绘制的连接结构</div>
        </div>
        <div class="mt-4 text-xs text-[var(--text-tertiary)]">结构信息</div>
        <dl class="mt-2 grid grid-cols-3 gap-2 text-xs">
          <div class="flex justify-between gap-3"><dt>原子数</dt><dd>{{ structureAtoms.length }}</dd></div>
          <div class="flex justify-between gap-3"><dt>元素数</dt><dd>{{ elementCount }}</dd></div>
          <div class="flex justify-between gap-3"><dt>格式</dt><dd class="uppercase">{{ prepared?.source_format || '—' }}</dd></div>
        </dl>
        <div class="mt-3 text-[11px] text-[var(--text-tertiary)]">二维键型来自文件记录；缺失时依据原子间距推断。</div>
      </aside>
      <div ref="threeContainer" class="relative min-w-0 flex-1 overflow-hidden bg-[#080b12]">
        <div ref="container" class="pointer-events-none absolute h-px w-px overflow-hidden opacity-0" aria-hidden="true" />
      <div v-if="status" class="absolute left-3 top-3 z-10 rounded bg-black/70 px-2 py-1 text-xs text-white">{{ status }}</div>
      <div v-if="selectedAtom" class="absolute bottom-3 left-3 z-10 max-w-[260px] rounded border bg-white/95 p-2 text-xs shadow">
        <div class="font-medium">原子 {{ selectedAtom.elem || '?' }}{{ selectedAtom.serial ?? selectedAtom.index ?? '' }}</div>
        <div>坐标: {{ format(selectedAtom.x) }}, {{ format(selectedAtom.y) }}, {{ format(selectedAtom.z) }}</div>
        <div v-if="distance">与第二个原子距离: {{ distance.toFixed(3) }} Å</div>
        <button v-if="distance" type="button" class="mt-1 text-[var(--text-secondary)] underline" @click="clearSelection">清除测距</button>
      </div>
    </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import * as $3Dmol from '3dmol';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { getFileDownloadUrl, prepareMolecularPreview, type FileInfo, type MolecularPreviewPreparation } from '../../api/file';
import { usePreviewLoad } from '../../composables/usePreviewLoad';

const props = defineProps<{ file: FileInfo }>();
const container = ref<HTMLElement | null>(null);
const threeContainer = ref<HTMLElement | null>(null);
const status = ref('');
const prepared = ref<MolecularPreviewPreparation>();
const styleMode = ref('ball-stick');
const showHydrogen = ref(true);
const showUnitCell = ref(false);
const selectedAtom = ref<$3Dmol.AtomSpec>();
const distance = ref<number>();
const structureAtoms = ref<$3Dmol.AtomSpec[]>([]);
const formulaText = ref('');
const elementCount = computed(() => new Set(structureAtoms.value.map((atom) => atom.elem).filter(Boolean)).size);
const structure2dAtoms = ref<Array<{ key: string; x: number; y: number; elem: string; color: string; showLabel: boolean }>>([]);
const structure2dBonds = ref<Array<{ key: string; x1: number; y1: number; x2: number; y2: number; order: number; offsetX: number; offsetY: number }>>([]);
let viewer: $3Dmol.GLViewer | null = null;
let model: $3Dmol.GLModel | null = null;
let resizeObserver: ResizeObserver | null = null;
const loads = usePreviewLoad();
let selected: $3Dmol.AtomSpec[] = [];
let threeRenderer: THREE.WebGLRenderer | null = null;
let threeScene: THREE.Scene | null = null;
let threeCamera: THREE.PerspectiveCamera | null = null;
let threeControls: OrbitControls | null = null;
let threeFrame = 0;
let threeClickHandler: ((event: PointerEvent) => void) | null = null;

const format = (value?: number) => value == null ? '-' : value.toFixed(3);
const isProtein = ref(false);
const parserFormat = (value: string) => value === 'mol' ? 'sdf' : value;

function applyStyle() {
  if (structureAtoms.value.length) renderThree(structureAtoms.value);
  if (!viewer || !model) return;
  const selection: $3Dmol.AtomSelectionSpec = showHydrogen.value ? {} : { not: { elem: 'H' } };
  const style: Record<string, any> = {};
  model.setStyle({}, {});
  if (styleMode.value === 'ball-stick') {
    style.sphere = { scale: 0.28 };
    style.stick = { radius: 0.16 };
  }
  else style[styleMode.value] = {};
  model.setStyle(selection, style);
  viewer.render();
}

const elementColors: Record<string, number> = {
  H: 0xffffff, C: 0x909090, N: 0x3050f8, O: 0xff0d0d, F: 0x90e050,
  P: 0xff8000, S: 0xffff30, Cl: 0x1ff01f, Br: 0xa62929, I: 0x940094,
  Na: 0xab5cf2, K: 0x8f40d4, Ca: 0x3dff00, Fe: 0xe06633, Mg: 0x8aff00,
};
const covalentRadii: Record<string, number> = {
  H: 0.31, C: 0.76, N: 0.71, O: 0.66, F: 0.57, P: 1.07, S: 1.05,
  Cl: 1.02, Br: 1.20, I: 1.39, Na: 1.66, K: 2.03, Ca: 1.76, Fe: 1.24, Mg: 1.41,
};

function clearThree() {
  cancelAnimationFrame(threeFrame);
  threeControls?.dispose();
  if (threeClickHandler && threeRenderer) threeRenderer.domElement.removeEventListener('pointerup', threeClickHandler);
  threeScene?.traverse((object) => {
    const mesh = object as THREE.Mesh;
    mesh.geometry?.dispose?.();
    const material = mesh.material as THREE.Material | THREE.Material[] | undefined;
    if (Array.isArray(material)) material.forEach((item) => item.dispose());
    else material?.dispose?.();
  });
  threeRenderer?.dispose();
  threeRenderer?.domElement.remove();
  threeRenderer = null;
  threeScene = null;
  threeCamera = null;
  threeControls = null;
  threeClickHandler = null;
}

function addBond(group: THREE.Group, start: THREE.Vector3, end: THREE.Vector3) {
  const direction = end.clone().sub(start);
  const length = direction.length();
  if (!length) return;
  if (styleMode.value === 'line') {
    const geometry = new THREE.BufferGeometry().setFromPoints([start, end]);
    group.add(new THREE.Line(geometry, new THREE.LineBasicMaterial({ color: 0xcbd5e1 })));
    return;
  }
  const geometry = new THREE.CylinderGeometry(styleMode.value === 'stick' ? 0.14 : 0.09, styleMode.value === 'stick' ? 0.14 : 0.09, length, 18);
  const mesh = new THREE.Mesh(geometry, new THREE.MeshPhongMaterial({ color: 0xd7dee8, shininess: 80 }));
  mesh.position.copy(start).add(end).multiplyScalar(0.5);
  mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize());
  group.add(mesh);
}

function inferBonds(atoms: $3Dmol.AtomSpec[], positions: THREE.Vector3[]) {
  const bonds: Array<{ i: number; j: number; order: number }> = [];
  const known = new Set<string>();
  const indexByAtomIndex = new Map<number, number>();
  atoms.forEach((atom, index) => {
    if (typeof atom.index === 'number') indexByAtomIndex.set(atom.index, index);
  });
  atoms.forEach((atom, i) => {
    const atomBonds = (atom as $3Dmol.AtomSpec & { bonds?: number[]; bondOrder?: number[] }).bonds || [];
    const orders = (atom as $3Dmol.AtomSpec & { bondOrder?: number[] }).bondOrder || [];
    atomBonds.forEach((target, bondIndex) => {
      const j = indexByAtomIndex.get(target) ?? (target >= 0 && target < atoms.length ? target : -1);
      if (j < 0 || j === i) return;
      const key = `${Math.min(i, j)}-${Math.max(i, j)}`;
      if (!known.has(key)) {
        known.add(key);
        bonds.push({ i, j, order: Math.max(1, Math.round(orders[bondIndex] || 1)) });
      }
    });
  });
  if (bonds.length) return bonds;
  for (let i = 0; i < atoms.length; i += 1) {
    for (let j = i + 1; j < atoms.length; j += 1) {
      const distance = positions[i].distanceTo(positions[j]);
      const cutoff = ((covalentRadii[atoms[i].elem || ''] || 0.8) + (covalentRadii[atoms[j].elem || ''] || 0.8)) * 1.28;
      if (distance > 0.25 && distance <= cutoff) bonds.push({ i, j, order: 1 });
    }
  }
  return bonds;
}

function renderStructure2d(atoms: $3Dmol.AtomSpec[]) {
  const heavyAtoms = atoms.filter((atom) => atom.elem !== 'H' && [atom.x, atom.y, atom.z].every((value) => typeof value === 'number'));
  const source = heavyAtoms.length ? heavyAtoms : atoms.filter((atom) => [atom.x, atom.y, atom.z].every((value) => typeof value === 'number'));
  if (!source.length) {
    structure2dAtoms.value = [];
    structure2dBonds.value = [];
    return;
  }
  const vectors = source.map((atom) => new THREE.Vector3(Number(atom.x), Number(atom.y), Number(atom.z)));
  const coordinatePairs: Array<[keyof THREE.Vector3, keyof THREE.Vector3]> = [['x', 'y'], ['x', 'z'], ['y', 'z']];
  const [axisA, axisB] = coordinatePairs.reduce((best, pair) => {
    const rangeA = Math.max(...vectors.map((point) => Number(point[pair[0]]))) - Math.min(...vectors.map((point) => Number(point[pair[0]])));
    const rangeB = Math.max(...vectors.map((point) => Number(point[pair[1]]))) - Math.min(...vectors.map((point) => Number(point[pair[1]])));
    const score = rangeA * rangeB;
    return score > best.score ? { pair, score } : best;
  }, { pair: coordinatePairs[0], score: -1 }).pair;
  const valuesA = vectors.map((point) => Number(point[axisA]));
  const valuesB = vectors.map((point) => Number(point[axisB]));
  const minA = Math.min(...valuesA); const maxA = Math.max(...valuesA);
  const minB = Math.min(...valuesB); const maxB = Math.max(...valuesB);
  const scale = Math.min(420 / Math.max(maxA - minA, 1), 420 / Math.max(maxB - minB, 1));
  const points = source.map((atom, index) => ({
    key: `${atom.index ?? index}`,
    x: 50 + (valuesA[index] - minA) * scale,
    y: 470 - (valuesB[index] - minB) * scale,
    elem: atom.elem || 'C',
    color: `#${(elementColors[atom.elem || ''] ?? 0x111827).toString(16).padStart(6, '0')}`,
    showLabel: atom.elem !== 'C',
  }));
  const bonds = inferBonds(source, vectors).map(({ i, j, order }) => {
    const a = points[i]; const b = points[j];
    const length = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    return { key: `${i}-${j}`, x1: a.x, y1: a.y, x2: b.x, y2: b.y, order, offsetX: -(b.y - a.y) / length * 5, offsetY: (b.x - a.x) / length * 5 };
  });
  structure2dAtoms.value = points;
  structure2dBonds.value = bonds;
}

function renderThree(atoms: $3Dmol.AtomSpec[]) {
  if (!threeContainer.value) return;
  clearThree();
  const visibleAtoms = atoms.filter((atom) => showHydrogen.value || atom.elem !== 'H');
  if (!visibleAtoms.length) {
    status.value = '结构中没有可显示的原子';
    return;
  }
  const width = Math.max(threeContainer.value.clientWidth, 320);
  const height = Math.max(threeContainer.value.clientHeight, 420);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x080b12);
  const camera = new THREE.PerspectiveCamera(42, width / height, 0.01, 5000);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(width, height, false);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.className = 'block h-full w-full cursor-grab active:cursor-grabbing';
  renderer.domElement.setAttribute('aria-label', '可拖动旋转的三维分子结构');
  threeContainer.value.prepend(renderer.domElement);
  scene.add(new THREE.AmbientLight(0xffffff, 1.8));
  const keyLight = new THREE.DirectionalLight(0xffffff, 3.2);
  keyLight.position.set(8, 12, 16);
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0x8ab4ff, 1.6);
  fillLight.position.set(-12, -6, 4);
  scene.add(fillLight);
  const group = new THREE.Group();
  scene.add(group);
  const positions = visibleAtoms.map((atom) => new THREE.Vector3(Number(atom.x), Number(atom.y), Number(atom.z)));
  const bounds = new THREE.Box3().setFromPoints(positions);
  const center = bounds.getCenter(new THREE.Vector3());
  positions.forEach((position) => position.sub(center));
  const atomMeshes: THREE.Mesh[] = [];
  visibleAtoms.forEach((atom, index) => {
    const elem = atom.elem || 'C';
    const baseRadius = covalentRadii[elem] || 0.8;
    const scale = styleMode.value === 'sphere' ? 0.72 : styleMode.value === 'stick' ? 0.24 : styleMode.value === 'line' ? 0.16 : 0.42;
    const geometry = new THREE.SphereGeometry(Math.max(baseRadius * scale, 0.13), 32, 24);
    const material = new THREE.MeshPhongMaterial({ color: elementColors[elem] ?? 0x36cfc9, shininess: 110, specular: 0x777777 });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.copy(positions[index]);
    mesh.userData.atom = atom;
    group.add(mesh);
    atomMeshes.push(mesh);
  });
  if (styleMode.value !== 'sphere') inferBonds(visibleAtoms, positions).forEach(({ i, j }) => addBond(group, positions[i], positions[j]));
  const size = Math.max(bounds.getSize(new THREE.Vector3()).length(), 2);
  camera.position.set(size * 1.25, size * 0.85, size * 1.65);
  camera.lookAt(0, 0, 0);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = Math.max(size * 0.25, 0.5);
  controls.maxDistance = size * 12;
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  threeClickHandler = (event: PointerEvent) => {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(atomMeshes, false)[0];
    if (hit?.object.userData.atom) selectAtom(hit.object.userData.atom as $3Dmol.AtomSpec);
  };
  renderer.domElement.addEventListener('pointerup', threeClickHandler);
  const animate = () => {
    threeFrame = requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  };
  animate();
  threeRenderer = renderer;
  threeScene = scene;
  threeCamera = camera;
  threeControls = controls;
  status.value = '';
}

function redrawUnitCell() {
  if (!viewer) return;
  viewer.removeAllShapes();
  if (showUnitCell.value && prepared.value?.supports_unit_cell && model) viewer.addUnitCell(model);
  viewer.render();
}

function clearSelection() {
  selected = [];
  selectedAtom.value = undefined;
  distance.value = undefined;
  viewer?.removeAllLabels();
  viewer?.removeAllShapes();
  redrawUnitCell();
  viewer?.render();
}

function selectAtom(atom: $3Dmol.AtomSpec) {
  selected.push(atom);
  if (selected.length > 2) selected = [atom];
  selectedAtom.value = atom;
  viewer?.removeAllLabels();
  viewer?.removeAllShapes();
  redrawUnitCell();
  if (selected.length === 2) {
    const [a, b] = selected;
    if ([a.x, a.y, a.z, b.x, b.y, b.z].every((v) => typeof v === 'number')) {
      distance.value = Math.sqrt((a.x! - b.x!) ** 2 + (a.y! - b.y!) ** 2 + (a.z! - b.z!) ** 2);
      viewer?.addLine({ start: { x: a.x!, y: a.y!, z: a.z! }, end: { x: b.x!, y: b.y!, z: b.z! }, color: 'red', dashed: true });
      viewer?.addLabel(`${distance.value.toFixed(3)} Å`, { position: { x: (a.x! + b.x!) / 2, y: (a.y! + b.y!) / 2, z: (a.z! + b.z!) / 2 }, backgroundColor: 'red', fontColor: 'white', fontSize: 12 });
    }
  }
  viewer?.render();
}

function parseCifFallback(text: string): $3Dmol.AtomSpec[] {
  const lines = text.split(/\r?\n/);
  const headers: string[] = [];
  let inLoop = false;
  let atomLoop = false;
  const atoms: $3Dmol.AtomSpec[] = [];
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    if (line.toLowerCase() === 'loop_') {
      inLoop = true;
      atomLoop = false;
      headers.length = 0;
      continue;
    }
    if (inLoop && line.startsWith('_')) {
      headers.push(line.split(/\s+/)[0]);
      if (line.startsWith('_atom_site_')) atomLoop = true;
      continue;
    }
    if (!inLoop || !atomLoop || line.startsWith('_') || line.startsWith('data_')) continue;
    const values = line.match(/(?:'[^']*'|"[^"]*"|\S+)/g) || [];
    if (values.length < headers.length) continue;
    const value = (name: string) => {
      const index = headers.findIndex((header) => header === name);
      return index >= 0 ? values[index]?.replace(/^['"]|['"]$/g, '') : undefined;
    };
    const elem = value('_atom_site_type_symbol') || value('_atom_site_label')?.replace(/[0-9].*$/, '');
    const x = Number(value('_atom_site_Cartn_x') ?? value('_atom_site_fract_x'));
    const y = Number(value('_atom_site_Cartn_y') ?? value('_atom_site_fract_y'));
    const z = Number(value('_atom_site_Cartn_z') ?? value('_atom_site_fract_z'));
    if (elem && [x, y, z].every(Number.isFinite)) atoms.push({ elem, x, y, z, index: atoms.length });
  }
  return atoms;
}

function parseXyzFallback(text: string): $3Dmol.AtomSpec[] {
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/);
  const declaredCount = Number.parseInt(lines[0]?.trim() || '', 10);
  const candidates = Number.isFinite(declaredCount) ? lines.slice(2, 2 + declaredCount) : lines;
  const atoms: $3Dmol.AtomSpec[] = [];
  for (const line of candidates) {
    const fields = line.trim().split(/\s+/);
    if (fields.length < 4 || !/^[A-Za-z]{1,3}$/.test(fields[0])) continue;
    const [x, y, z] = fields.slice(1, 4).map(Number);
    if (![x, y, z].every(Number.isFinite)) continue;
    const elem = fields[0][0].toUpperCase() + fields[0].slice(1).toLowerCase();
    atoms.push({ elem, x, y, z, index: atoms.length });
  }
  return atoms;
}

function parsePdbFallback(text: string): $3Dmol.AtomSpec[] {
  const atoms: $3Dmol.AtomSpec[] = [];
  for (const line of text.split(/\r?\n/)) {
    if (!['ATOM  ', 'HETATM'].includes(line.slice(0, 6))) continue;
    const x = Number(line.slice(30, 38)); const y = Number(line.slice(38, 46)); const z = Number(line.slice(46, 54));
    const rawElem = line.slice(76, 78).trim() || line.slice(12, 16).trim().replace(/[0-9]/g, '');
    if (!rawElem || ![x, y, z].every(Number.isFinite)) continue;
    const elem = rawElem[0].toUpperCase() + rawElem.slice(1).toLowerCase();
    atoms.push({ elem, x, y, z, index: atoms.length, serial: Number(line.slice(6, 11)) || atoms.length + 1 });
  }
  return atoms;
}

function parseTextFallback(text: string, sourceFormat: string) {
  if (sourceFormat === 'xyz') return parseXyzFallback(text);
  if (sourceFormat === 'cif') return parseCifFallback(text);
  if (sourceFormat === 'pdb') return parsePdbFallback(text);
  return [];
}

function resolveFormula(text: string, atoms: $3Dmol.AtomSpec[]) {
  const declared = text.match(/^_chemical_formula_(?:sum|structural)\s+(.+)$/mi)?.[1]
    ?.trim().replace(/^['"]|['"]$/g, '').replace(/\s+/g, '');
  if (declared) return declared.replace(/([A-Za-z])1(?=[A-Z]|$)/g, '$1');
  const counts = new Map<string, number>();
  atoms.forEach((atom) => {
    if (atom.elem) counts.set(atom.elem, (counts.get(atom.elem) || 0) + 1);
  });
  return [...counts.entries()]
    .sort(([a], [b]) => (a === 'C' ? -1 : b === 'C' ? 1 : a === 'H' ? -1 : b === 'H' ? 1 : a.localeCompare(b)))
    .map(([elem, count]) => `${elem}${count > 1 ? count : ''}`).join('');
}

async function render(file: FileInfo) {
  const load = loads.begin();
  resizeObserver?.disconnect();
  viewer?.clear();
  viewer = null;
  model = null;
  status.value = '';
  structureAtoms.value = [];
  formulaText.value = '';
  structure2dAtoms.value = [];
  structure2dBonds.value = [];
  clearThree();
  prepared.value = undefined;
  clearSelection();
  if (!file?.file_id || !container.value) return;
  status.value = '正在加载结构...';
  try {
    const metadata = await prepareMolecularPreview(file.file_id);
    load.assertCurrent();
    const url = await getFileDownloadUrl(file);
    load.assertCurrent();
    const response = await fetch(url, { signal: load.signal });
    if (!response.ok) throw new Error(`下载失败 (${response.status})`);
    const text = await response.text();
    if (!load.isCurrent() || !container.value) return;
    prepared.value = metadata;
    // FilePanel can mount this component before its flex parent has a size.
    // Wait for layout, then force 3Dmol to measure the real canvas dimensions.
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    if (!load.isCurrent() || !container.value) return;
    const format = parserFormat(metadata.source_format);
    let atoms: $3Dmol.AtomSpec[] = [];
    try {
      viewer = $3Dmol.createViewer(container.value, { backgroundColor: 'white' });
      try {
        model = viewer.addModel(text, format);
      } catch (parseError) {
        if (format !== 'cif') throw parseError;
        model = viewer.addModel(text);
      }
      if ((!model || model.selectedAtoms({}).length === 0) && format === 'cif') {
        viewer.removeAllModels();
        model = viewer.addModel(text);
      }
      atoms = (model?.selectedAtoms({}) || []) as $3Dmol.AtomSpec[];
    } catch (parseError) {
      console.warn('3Dmol parser failed, using built-in text parser:', parseError);
    }
    if (!atoms.some((atom) => [atom.x, atom.y, atom.z].every((value) => typeof value === 'number'))) atoms = parseTextFallback(text, format);
    if (!atoms.length) throw new Error(`未能从 ${format.toUpperCase()} 文件解析出原子坐标`);
    isProtein.value = metadata.source_format === 'pdb';
    if (model && viewer) {
      model.setClickable({}, true, (atom: $3Dmol.AtomSpec) => selectAtom(atom));
      viewer.resize();
      viewer.zoomTo();
      viewer.render();
    }
    structureAtoms.value = atoms;
    formulaText.value = resolveFormula(text, atoms);
    renderStructure2d(atoms);
    renderThree(atoms);
    resizeObserver = new ResizeObserver(() => {
      if (!threeContainer.value || !threeRenderer || !threeCamera) return;
      const width = Math.max(threeContainer.value.clientWidth, 320);
      const height = Math.max(threeContainer.value.clientHeight, 420);
      threeCamera.aspect = width / height;
      threeCamera.updateProjectionMatrix();
      threeRenderer.setSize(width, height, false);
    });
    if (threeContainer.value) resizeObserver.observe(threeContainer.value);
    if (!threeRenderer) throw new Error('浏览器未能创建三维渲染画布');
  } catch (error) {
    if (!load.isCurrent()) return;
    console.error('Failed to render molecular structure:', error);
    status.value = error instanceof Error ? error.message : '结构预览失败';
  }
}

function resetView() {
  if (!threeCamera || !threeControls || !structureAtoms.value.length) return;
  const points = structureAtoms.value.map((atom) => new THREE.Vector3(Number(atom.x), Number(atom.y), Number(atom.z)));
  const size = Math.max(new THREE.Box3().setFromPoints(points).getSize(new THREE.Vector3()).length(), 2);
  threeCamera.position.set(size * 1.25, size * 0.85, size * 1.65);
  threeControls.target.set(0, 0, 0);
  threeControls.update();
}
function exportPng() {
  if (!threeRenderer || !threeScene || !threeCamera) return;
  threeRenderer.render(threeScene, threeCamera);
  const link = document.createElement('a');
  link.href = threeRenderer.domElement.toDataURL('image/png');
  link.download = `${props.file.filename.replace(/\.[^.]+$/, '') || 'structure'}.png`;
  link.click();
}

onMounted(() => render(props.file));
watch(() => props.file, (file) => render(file));
watch([styleMode, showHydrogen], applyStyle);
watch(showUnitCell, redrawUnitCell);
onBeforeUnmount(() => { loads.dispose(); resizeObserver?.disconnect(); viewer?.clear(); clearThree(); });
</script>
