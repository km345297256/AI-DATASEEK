import { ref, watch, onUnmounted, type Ref } from 'vue';
import { getDataRuns } from '@/api/agent';
import type { DataRun } from '@/types/dataRun';

export function useDataRuns(session: Ref<string | undefined>, revision: Ref<number | undefined>) {
  const runs = ref<DataRun[]>([]);
  const nextBefore = ref<number | null>(null);
  const loading = ref(false);
  const error = ref('');
  let request: AbortController | undefined;
  let generation = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  async function load(append = false) {
    if (!session.value) return;
    request?.abort();
    const controller = new AbortController();
    request = controller;
    const current = ++generation;
    loading.value = true;
    error.value = '';
    try {
      const page = await getDataRuns(session.value, append ? nextBefore.value ?? undefined : undefined, controller.signal);
      if (current !== generation) return;
      const combined = append ? [...runs.value, ...page.runs] : page.runs;
      runs.value = [...new Map(combined.map(run => [run.run_id, run])).values()];
      nextBefore.value = page.next_before_seq;
    } catch {
      if (current === generation && !controller.signal.aborted) error.value = '任务记录暂时无法读取，请刷新重试。';
    } finally {
      if (current === generation) loading.value = false;
    }
  }
  watch(session, () => {
    ++generation;
    request?.abort();
    clearTimeout(timer);
    timer = undefined;
    runs.value = [];
    nextBefore.value = null;
    loading.value = false;
    error.value = '';
    void load();
  }, { immediate: true });
  watch(revision, () => {
    if (!session.value || timer) return;
    timer = setTimeout(() => { timer = undefined; void load(); }, 1500);
  });
  onUnmounted(() => { ++generation; request?.abort(); clearTimeout(timer); });
  return { runs, nextBefore, loading, error, load };
}
