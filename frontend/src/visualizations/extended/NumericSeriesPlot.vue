<template><div ref="target" class="min-w-0 w-full" :aria-label="label" /><p v-if="error" role="alert" class="p-3 text-amber-700">{{ error }}</p></template>
<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import { usePreviewLoad } from '../../composables/usePreviewLoad';
import { loadBrowserLibrary } from './scientific/browserLibraries';
import type { NumericTrace } from './instrumentData';
const props = defineProps<{ traces: NumericTrace[]; xLabel: string; label: string; xRange?: [number, number] }>();
const target = ref<HTMLDivElement>(), error = ref(''), scope = usePreviewLoad();
async function draw() {
  const load = scope.begin(); error.value = '';
  try {
    if (!props.traces.length || props.traces.length > 8) return;
    const library = await loadBrowserLibrary('plotly', load.signal);
    await nextTick(); load.assertCurrent(); if (!target.value) return;
    // A render owns a distinct element. A late async draw cannot purge a newer plot.
    const element = document.createElement('div'); target.value.replaceChildren(element);
    element.style.height = `${Math.max(320, props.traces.length * 240)}px`;
    load.onDispose(() => { library.purge(element); element.remove(); });
    const traces = props.traces.map((trace, index) => ({ type: 'scatter' as const, mode: trace.x.length === 1 ? 'markers' as const : 'lines' as const,
      name: trace.name, x: trace.x, y: trace.y, xaxis: index ? `x${index + 1}` : 'x', yaxis: index ? `y${index + 1}` : 'y', connectgaps: false }));
    const layout: Partial<import('plotly.js').Layout> = { margin: { l: 90, r: 30, t: 30, b: 55 }, showlegend: false,
      grid: { rows: traces.length, columns: 1, pattern: 'independent', roworder: 'top to bottom' } };
    props.traces.forEach((trace, index) => {
      Object.assign(layout, { [`xaxis${index ? index + 1 : ''}`]: { title: { text: props.xLabel }, automargin: true, ...(props.xRange ? { range: props.xRange } : {}) },
        [`yaxis${index ? index + 1 : ''}`]: { title: { text: `${trace.name} (${trace.unit})` }, automargin: true } });
    });
    await library.newPlot(element, traces, layout, { responsive: true, displaylogo: false, displayModeBar: false });
    if (!load.isCurrent()) { library.purge(element); element.remove(); return; }
    if (typeof ResizeObserver !== 'undefined') { const observer = new ResizeObserver(() => { if (load.isCurrent()) void library.Plots.resize(element); }); observer.observe(element); load.onDispose(() => observer.disconnect()); }
  } catch (reason) { if (load.isCurrent()) error.value = reason instanceof Error ? reason.message : '曲线绘制失败。'; }
}
watch([() => props.traces, () => props.xLabel, () => props.xRange], () => { void draw(); }, { immediate: true });
</script>
