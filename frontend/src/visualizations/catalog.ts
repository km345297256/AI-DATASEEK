import { readonly, ref, shallowReadonly, shallowRef } from 'vue';
import { getVisualizationCatalog, setVisualizationEnabled } from '../api/visualization';
import type { VisualizationCatalog } from './contract';

const catalog = shallowRef<VisualizationCatalog | null>(null);
const loading = ref(false);
const error = ref('');
const updating = ref<string | null>(null);
let generation = 0;

export function useVisualizationCatalog() {
  const refresh = async () => {
    // Do not let a periodic poll race a state mutation.
    if (updating.value) return;
    const current = ++generation;
    loading.value = true;
    try {
      const result = await getVisualizationCatalog();
      if (current !== generation) return;
      catalog.value = result;
      error.value = '';
    } catch {
      if (current !== generation) return;
      catalog.value = null;
      error.value = '无法连接 Cordis 可视化插件目录。预览已暂停，请重试；文件仍可下载。';
    } finally {
      if (current === generation) loading.value = false;
    }
  };
  const toggle = async (id: string, enabled: boolean) => {
    if (updating.value) return;
    const current = ++generation;
    updating.value = id;
    try {
      const result = await setVisualizationEnabled(id, enabled);
      if (current !== generation) return;
      catalog.value = result;
      error.value = '';
    } catch {
      catalog.value = null;
      error.value = '插件状态未能确认，预览已暂停。请刷新插件目录核实状态。';
    } finally {
      updating.value = null;
      loading.value = false;
    }
  };
  return { catalog: shallowReadonly(catalog), loading: readonly(loading), error: readonly(error), updating: readonly(updating), refresh, toggle };
}
