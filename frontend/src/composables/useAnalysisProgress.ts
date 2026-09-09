import { ref, type Ref } from 'vue';
import type { AgentSSEEvent, MessageEventData } from '../types/event';
import { isAnalysisProgressEvent, nextAnalysisProgress } from '../utils/analysisProgress.ts';

/** One ephemeral status per view. A terminal result cannot be reopened by a late hint. */
export function useAnalysisProgress(analysisProgress: Ref<string> = ref('')) {
  let closed = false;
  const beginAnalysisProgress = () => {
    closed = false;
    analysisProgress.value = '';
  };
  const clearAnalysisProgress = () => {
    closed = true;
    analysisProgress.value = '';
  };
  const updateAnalysisProgress = (event: AgentSSEEvent) => {
    const message = event.event === 'message' ? event.data as MessageEventData : undefined;
    if (message?.role === 'user') {
      beginAnalysisProgress();
      return;
    }
    if (['done', 'wait', 'error'].includes(event.event) || message?.metadata?.analysis_outcome) {
      clearAnalysisProgress();
      return;
    }
    if (closed && isAnalysisProgressEvent(event)) return;
    analysisProgress.value = nextAnalysisProgress(analysisProgress.value, event);
  };
  return { analysisProgress, updateAnalysisProgress, beginAnalysisProgress, clearAnalysisProgress };
}
