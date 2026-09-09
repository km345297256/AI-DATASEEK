import { ref, watch } from 'vue';
import type { FileInfo, FilePreviewPage } from '../api/file.ts';
import { getPluginPage } from '../visualizations/runtime';
import type { VisualizationPlugin } from '../visualizations/contract';
import { usePreviewLoad } from './usePreviewLoad.ts';

/** Hold only the current bounded page; previous pages retain offsets, not data. */
export function useFilePreviewPages(file: () => FileInfo, plugin: () => VisualizationPlugin) {
  const loads = usePreviewLoad();
  const page = ref<FilePreviewPage>();
  const headers = ref<string[]>([]);
  const loading = ref(false);
  const error = ref('');
  const pageIndex = ref(0);
  let offsets = [{ offset: 0, header_pending: true }];
  let version: string | undefined;
  let delimiter: ',' | '\t' | undefined;

  async function loadPage(index: number) {
    const request = loads.begin();
    const selectedFile = file();
    const cursor = offsets[index];
    if (!selectedFile?.file_id || cursor === undefined) return;
    loading.value = true;
    error.value = '';
    try {
      const result = await getPluginPage(selectedFile, plugin(), { ...cursor, version, delimiter }, request.signal);
      if (!request.isCurrent()) return;
      page.value = result;
      pageIndex.value = index;
      version = result.version;
      delimiter = result.header_pending ? undefined : result.delimiter || undefined;
      if (index === 0 || result.headers.length) headers.value = result.headers;
      offsets = offsets.slice(0, index + 1);
      if (result.next_offset !== null) offsets.push({ offset: result.next_offset, header_pending: result.header_pending });
    } catch (cause) {
      if (!request.isCurrent()) return;
      const status = (cause as { status?: number })?.status;
      error.value = status === 409
        ? '文件已更新，请重新打开预览。'
        : '无法预览此页。请使用 UTF-8 文本或 CSV（单条记录不超过 128 KiB），也可以下载完整文件。';
    } finally {
      if (request.isCurrent()) loading.value = false;
    }
  }

  watch(file, () => {
    page.value = undefined;
    headers.value = [];
    pageIndex.value = 0;
    offsets = [{ offset: 0, header_pending: true }];
    version = undefined;
    delimiter = undefined;
    void loadPage(0);
  }, { immediate: true });

  return { page, headers, loading, error, pageIndex, loadPage };
}
