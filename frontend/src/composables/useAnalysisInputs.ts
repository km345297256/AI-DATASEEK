import { onScopeDispose, ref, watch, type Ref } from 'vue';
import type { FileInfo } from '../api/file';
import { getSessionAnalysisInputs } from '../api/agent';
import { toggleInputFile } from '../utils/analysisInputs';

/** Inputs are server-authorized session resources, never inferred from output attachments. */
export function useAnalysisInputs(sessionId: Ref<string | null | undefined>, running: Ref<boolean>) {
  const files = ref<FileInfo[]>([]);
  const selectedIds = ref<string[] | undefined>();
  const loading = ref(false);
  const error = ref('');
  let generation = 0;
  let controller: AbortController | undefined;
  let disposed = false;

  async function refresh() {
    const id = sessionId.value;
    const requestGeneration = ++generation;
    controller?.abort();
    if (!id || disposed) { loading.value = false; return; }
    const request = new AbortController();
    controller = request;
    loading.value = true;
    error.value = '';
    try {
      const result = await getSessionAnalysisInputs(id, request.signal);
      if (disposed || generation !== requestGeneration || sessionId.value !== id) return;
      files.value = result.files;
      selectedIds.value = [...result.selected_file_ids];
    } catch (cause) {
      if (disposed || generation !== requestGeneration || request.signal.aborted) return;
      error.value = '暂时无法确认分析资料，请重新加载。';
    } finally {
      if (generation === requestGeneration) loading.value = false;
    }
  }
  function toggle(fileId: string) {
    if (loading.value || running.value || error.value) return;
    selectedIds.value = toggleInputFile(selectedIds.value || [], fileId, files.value);
  }
  watch(sessionId, () => {
    files.value = [];
    selectedIds.value = undefined;
    error.value = '';
    void refresh();
  // Reset synchronously: a pending first message may be submitted in the same
  // turn as a route/session change, before Vue's batched watchers have flushed.
  }, { immediate: true, flush: 'sync' });
  watch(running, (active, previous) => { if (!active && previous) void refresh(); });
  onScopeDispose(() => { disposed = true; generation += 1; controller?.abort(); });
  return { files, selectedIds, loading, error, refresh, toggle };
}
