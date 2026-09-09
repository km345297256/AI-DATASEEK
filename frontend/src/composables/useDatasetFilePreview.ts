import { computed, onMounted, onScopeDispose, ref, watch } from 'vue';
import { prepareDatasetFilePreview, type DataCenterDataset } from '../api/dataset';
import { useVisualizationCatalog } from '../visualizations/catalog';
import { matchingVisualizations } from '../visualizations/contract';
import { useFilePanel } from './useFilePanel';
import { showErrorToast } from '../utils/toast';

/** A page-scoped controller: one catalog poll, no file reads until the user clicks. */
export function useDatasetFilePreview(dataset: () => DataCenterDataset | undefined, onOpened: () => void = () => {}) {
  const { catalog, error, loading, refresh } = useVisualizationCatalog();
  const { beginFilePreview } = useFilePanel();
  const pendingPath = ref<string | null>(null);
  const candidates = computed(() => new Map((dataset()?.files ?? []).map((file) => [
    file.path || file.name,
    matchingVisualizations(catalog.value?.plugins ?? [], file.path || file.name),
  ])));
  let controller: AbortController | undefined;
  let pendingPluginId: string | undefined;
  let disposed = false;
  let poll: ReturnType<typeof setInterval> | undefined;

  const cancel = () => {
    controller?.abort();
    controller = undefined;
    pendingPluginId = undefined;
    pendingPath.value = null;
  };
  const refreshVisible = () => {
    if (document.visibilityState !== 'hidden') void refresh();
  };

  async function preview(path: string) {
    if (disposed || pendingPath.value === path) return;
    const currentDataset = dataset();
    const plugin = candidates.value.get(path)?.[0];
    if (!currentDataset || !plugin) return;
    cancel();
    const request = beginFilePreview();
    const current = new AbortController();
    const revision = catalog.value?.revision;
    controller = current;
    pendingPluginId = plugin.id;
    pendingPath.value = path;
    const isCurrent = () => !disposed && !current.signal.aborted && request.isCurrent()
      && dataset()?.dataset_id === currentDataset.dataset_id
      && catalog.value?.revision === revision
      && candidates.value.get(path)?.some((item) => item.id === plugin.id);
    try {
      const prepared = await prepareDatasetFilePreview(currentDataset.dataset_id, path, plugin.id, current.signal);
      if (isCurrent() && request.show(prepared.file, prepared.related_files)) onOpened();
    } catch (cause) {
      if (isCurrent()) showErrorToast(cause instanceof Error ? cause.message : '文件预览准备失败，请稍后重试');
    } finally {
      if (controller === current) cancel();
    }
  }

  watch(() => dataset()?.dataset_id, cancel, { flush: 'sync' });
  // Stop pending preparation if a capability disappears, is disabled, or is replaced.
  watch(catalog, (next, previous) => {
    if (pendingPath.value && (next?.revision !== previous?.revision
      || !next?.plugins.some((plugin) => plugin.enabled && plugin.id === pendingPluginId))) cancel();
  }, { flush: 'sync' });
  onMounted(() => {
    void refresh();
    poll = setInterval(refreshVisible, 15000);
    window.addEventListener('focus', refreshVisible);
  });
  onScopeDispose(() => {
    disposed = true;
    cancel();
    if (poll) clearInterval(poll);
    window.removeEventListener('focus', refreshVisible);
  });
  return { candidates, pendingPath, preview, error, loading, refresh };
}
