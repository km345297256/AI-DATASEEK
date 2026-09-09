<script setup lang="ts">
import { ref } from 'vue';
import type { FileInfo } from '../../../api/file';
import type { VisualizationPlugin } from '../../contract';
import { loadPluginBytes } from '../runtime';
import { chromosomeSizes, checkedGenomicText } from './guards';
import { displayError, useDomainScope } from './lifecycle';
const props = defineProps<{ file: FileInfo; plugin: VisualizationPlugin }>();
const target = ref<HTMLElement>(); const error = ref(''); const reference = ref(''); const busy = ref(false); const ready = ref(false); const scope = useDomainScope();
async function start() {
  if (busy.value || ready.value) return;
  error.value = ''; busy.value = true;
  try {
    const refData = chromosomeSizes(reference.value);
    const bytes = await loadPluginBytes(props.file, props.plugin, scope.signal); scope.check();
    const format = props.file.filename.toLowerCase().endsWith('.vcf') ? 'vcf' : 'bed';
    const text = checkedGenomicText(bytes, format, refData.sizes);
    // The package's browser entry is an IIFE, not an ES module. Select its ESM build explicitly.
    const module = await import('igv/dist/igv.esm.js'); const igv = module.default; scope.check();
    const chromUrl = scope.blob(new Blob([refData.text], { type: 'text/plain' }));
    const trackUrl = scope.blob(new Blob([text], { type: 'text/plain' }));
    const browser = await igv.createBrowser(target.value!, {
      loadDefaultGenomes: false, genomeList: [], showIdeogram: false, showSequence: false, showCursorTrackingGuide: false,
      showSVGButton: false, showSearch: false, showChromosomeWidget: true,
      reference: { name: '用户明确提供的本地 chrom.sizes', format: 'chromsizes', url: chromUrl, wholeGenomeView: false },
      locus: `${refData.first}:1-${Math.min(refData.sizes.get(refData.first)!, 100000)}`,
      tracks: [{ name: format === 'vcf' ? '本地变异（核心字段）' : '本地注释', type: format === 'vcf' ? 'variant' : 'annotation', format, url: trackUrl, indexed: false, visibilityWindow: -1, displayMode: 'EXPANDED' }],
    } as any);
    scope.add(() => igv.removeBrowser(browser)); scope.check(); ready.value = true;
  } catch (reason) { if (!scope.signal.aborted) error.value = displayError(reason); }
  finally { busy.value = false; }
}
</script>
<template><section class="domain-preview"><p>IGV.js · BED / VCF 本地轨道。必须指定实际参考组装的染色体长度，系统不会猜测参考基因组或联网下载序列。</p><div v-if="!ready" class="reference"><label for="igv-chrom-sizes">参考 chrom.sizes（每行：染色体名称 TAB 长度）</label><textarea id="igv-chrom-sizes" v-model="reference" rows="4" maxlength="65536" placeholder="请粘贴与该文件参考组装一致的 chrom.sizes"/><button :disabled="busy" @click="start">{{ busy ? '加载中…' : '确认参考并显示轨道' }}</button></div><p v-if="error" role="alert">{{ error }}</p><div ref="target" class="surface"/><p>首期最多 50,000 条记录，仅显示注释区间 / VCF 核心变异字段；不提供参考碱基、基因型及 BAM/CRAM 比对。</p></section></template>
<style scoped>.domain-preview{height:100%;overflow:auto;min-height:360px}.surface{min-height:220px;width:100%;position:relative}.reference{display:flex;flex-direction:column;gap:8px;padding:12px}textarea{border:1px solid #bbb;border-radius:5px;font-family:monospace;padding:8px}button{background:#267c69;color:white;padding:8px;border-radius:4px}button:disabled{opacity:.5}p{font-size:12px;padding:8px;color:#667085}[role=alert]{color:#b42318}</style>
