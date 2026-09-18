import { nextTick, ref } from 'vue';
import type { FileInfo } from '../api/file';
import type { SSECallbacks } from '../api/client';
import type { AgentSSEEvent, CompletionAdviceData, DoneEventData, ErrorEventData, MessageEventData, PlanEventData, StepEventData, TitleEventData, ToolEventData } from '../types/event';
import type { AttachmentsContent, Message, MessageContent, StepContent, ToolContent } from '../types/message';
import type { AnalysisContinuationAttempt } from '../types/analysisOutcome';
import type { CreateSessionResponse, GetSessionHistoryResponse } from '../types/response';
import { findAnalysisTool, mergeAnalysisToolEvent } from '../utils/analysisJob.ts';
import { completeRunningSteps, failRunningSteps, findCurrentTurnRunningStep, findCurrentTurnStep, insertTaskExecutionSummary } from '../utils/chatTimeline.ts';
import { acceptAgentEvent, createAgentEventCursor, resetAgentEventCursor } from '../utils/agentEventCursor.ts';
import { createMessageKey, isLegacyPlanProgressMessage, prependHistoricalMessages, projectHistoryMessages } from '../utils/sessionHistory.ts';
import { isPlaceholderAssistantMessage } from '../utils/datasetResultPresentation.ts';
import { continuationAttempt, resumableAnalysisOutcome } from '../utils/analysisOutcome.ts';
import { isAnalysisProgressEvent, isAnalysisProgressMessage } from '../utils/analysisProgress.ts';
import { useAnalysisProgress } from './useAnalysisProgress.ts';

type SessionApi = Pick<typeof import('../api/agent'), 'chatWithSession' | 'createSession' | 'createClientMessageId' | 'getSessionHistory' | 'stopSession'>;

export interface AnalysisSessionRequest {
  message: string;
  files?: FileInfo[];
  skills?: string[];
  mcpServers?: string[];
  agentProfileId?: string | null;
  datasetIds?: string[];
  inputFileIds?: string[];
}

export interface AnalysisSessionOptions {
  api: SessionApi;
  getViewport?: () => HTMLElement | null | undefined;
  onNewTurn?: () => void;
  onReset?: () => void;
  onTerminal?: () => void;
  onSessionCreated?: (session: CreateSessionResponse) => void;
  onSessionRestored?: (session: GetSessionHistoryResponse) => void;
  onTitle?: (title: string) => void;
  onHistoryError?: () => void;
}

/** Both analysis entry points share transport, event projection and task lifecycle.
 * Sources are request data, never a second execution mode or duplicated controller.
 */
export function useAnalysisSession(options: AnalysisSessionOptions) {
  const { api } = options;
  const sessionId = ref<string>();
  const sessionCreatedAt = ref<number | null>(null);
  const messages = ref<Message[]>([]);
  const isLoading = ref(false);
  const connectionNotice = ref('');
  const loadingStatus = ref('');
  const hasMoreHistory = ref(false);
  const isLoadingHistory = ref(false);
  const isRestoringHistory = ref(false);
  const historyBeforeSeq = ref<number>();
  const timelineRevision = ref(0);
  const follow = ref(true);
  const plan = ref<PlanEventData>();
  const lastTool = ref<ToolContent>();
  const lastNoMessageTool = ref<ToolContent>();
  const completionAdvice = ref<CompletionAdviceData>();
  const selectedSkills = ref<string[]>([]);
  const selectedMcpServers = ref<string[]>([]);
  const lastEventId = ref<string>();
  const lastEventSeq = ref<number>();
  const taskStartedAtMs = ref<number>();
  const analysisContinuation = ref<AnalysisContinuationAttempt | null>(null);
  const cancelCurrentChat = ref<(() => void) | null>(null);
  const { analysisProgress, updateAnalysisProgress, beginAnalysisProgress, clearAnalysisProgress } = useAnalysisProgress();
  const messageKey = createMessageKey();
  const eventCursor = createAgentEventCursor();
  let generation = 0;
  let disposed = false;
  let stopping = false;
  let historyRequest: AbortController | undefined;

  const isCurrent = (revision: number, id = sessionId.value) => !disposed && revision === generation && id === sessionId.value;
  const terminal = (status: StepEventData['status']) => status === 'completed' || status === 'failed';

  function cancelTransport() {
    const cancel = cancelCurrentChat.value;
    cancelCurrentChat.value = null;
    cancel?.();
  }

  function invalidate() {
    generation += 1;
    stopping = false;
    historyRequest?.abort();
    historyRequest = undefined;
    isLoadingHistory.value = false;
    isRestoringHistory.value = false;
    cancelTransport();
    return generation;
  }

  function failActiveSteps(currentTurnOnly = true) {
    const failedIds = new Set(failRunningSteps(messages.value, currentTurnOnly).map(step => step.id));
    for (const step of plan.value?.steps ?? []) {
      if (failedIds.has(step.id) && step.status === 'running') step.status = 'failed';
    }
  }

  function startUserTurn() {
    beginAnalysisProgress();
    failActiveSteps(false);
    plan.value = undefined;
    lastTool.value = undefined;
    lastNoMessageTool.value = undefined;
    completionAdvice.value = undefined;
    follow.value = true;
    options.onNewTurn?.();
  }

  function handleMessage(data: MessageEventData) {
    if (isAnalysisProgressMessage(data)) return;
    if (data.role === 'user') startUserTurn();
    if (data.role === 'assistant' && (isPlaceholderAssistantMessage(data.content) || isLegacyPlanProgressMessage(data.content))) return;
    messages.value.push({ type: data.role, content: { ...data } as MessageContent });
    if (data.attachments?.length) messages.value.push({ type: 'attachments', content: { ...data } as AttachmentsContent });
  }

  function handleTool(data: ToolEventData) {
    let tool = { ...data } as ToolContent;
    const existing = findAnalysisTool(messages.value, tool.tool_call_id);
    if (existing) {
      Object.assign(existing, mergeAnalysisToolEvent(existing, tool));
      tool = existing;
    } else {
      const step = findCurrentTurnRunningStep(messages.value);
      if (step) step.tools.push(tool);
      else messages.value.push({ type: 'tool', content: tool });
      lastTool.value = tool;
    }
    if (tool.name !== 'message' && (!existing || lastTool.value?.tool_call_id === tool.tool_call_id)) lastNoMessageTool.value = tool;
  }

  function handleStep(data: StepEventData) {
    const planned = plan.value?.steps.find(step => step.id === data.id);
    if (planned && (!terminal(planned.status) || terminal(data.status))) {
      planned.status = data.status;
      planned.description = data.description;
    }
    const existing = findCurrentTurnStep(messages.value, data.id);
    if (existing) {
      if (terminal(existing.status) && !terminal(data.status)) return;
      Object.assign(existing, { status: data.status, description: data.description });
      if (terminal(data.status)) existing.ended_at = data.timestamp;
    } else if (data.status === 'running') {
      messages.value.push({ type: 'step', content: { ...data, started_at: data.timestamp, tools: [] } as StepContent });
    }
  }

  function handleEvent(event: AgentSSEEvent) {
    if (disposed || !acceptAgentEvent(eventCursor, event)) return;
    updateAnalysisProgress(event);
    if (!isAnalysisProgressEvent(event)) timelineRevision.value += 1;
    if (event.event === 'message') handleMessage(event.data as MessageEventData);
    else if (event.event === 'tool') handleTool(event.data as ToolEventData);
    else if (event.event === 'step') handleStep(event.data as StepEventData);
    else if (event.event === 'title') options.onTitle?.((event.data as TitleEventData).title);
    else if (event.event === 'plan') {
      const previous = new Map(plan.value?.steps.map(step => [step.id, step]));
      const data = event.data as PlanEventData;
      plan.value = { ...data, steps: data.steps.map(step => {
        const old = previous.get(step.id);
        return old && terminal(old.status) && !terminal(step.status) ? { ...step, status: old.status } : step;
      }) };
    } else if (event.event === 'error') {
      const data = event.data as ErrorEventData;
      messages.value.push({ type: 'assistant', content: { content: data.error, timestamp: data.timestamp } as MessageContent });
      failActiveSteps();
    } else if (event.event === 'done') {
      completeRunningSteps(messages.value, event.data.timestamp);
      const elapsed = taskStartedAtMs.value === undefined ? undefined : performance.now() - taskStartedAtMs.value;
      insertTaskExecutionSummary(messages.value, event.data.timestamp, elapsed);
      completionAdvice.value = (event.data as DoneEventData).advice;
    }
    if (['done', 'wait', 'error'].includes(event.event)) {
      isLoading.value = false;
      taskStartedAtMs.value = undefined;
      loadingStatus.value = '';
      options.onTerminal?.();
    }
    if (event.data.event_id) lastEventId.value = event.data.event_id;
    lastEventSeq.value = eventCursor.lastSeq;
  }

  function callbacks(revision: number, id: string): SSECallbacks<AgentSSEEvent['data']> {
    const current = () => isCurrent(revision, id);
    const close = () => {
      isLoading.value = false;
      clearAnalysisProgress();
      taskStartedAtMs.value = undefined;
      loadingStatus.value = '';
      cancelCurrentChat.value = null;
    };
    return {
      onOpen: () => { if (current()) { isLoading.value = true; connectionNotice.value = ''; } },
      onRetry: ({ attempt, maxAttempts }) => {
        if (current()) { isLoading.value = true; connectionNotice.value = `连接中断，正在恢复（${attempt}/${maxAttempts}）…`; }
      },
      onMessage: ({ event, data }) => { if (current()) handleEvent({ event: event as AgentSSEEvent['event'], data }); },
      onClose: () => { if (current()) { close(); connectionNotice.value = ''; options.onTerminal?.(); } },
      onError: (error) => {
        if (!current()) return;
        // Transport uncertainty is not a confirmed execution failure.
        close(); connectionNotice.value = error.message; options.onTerminal?.();
      },
    };
  }

  async function connect(revision: number, request?: AnalysisSessionRequest, continuation?: AnalysisContinuationAttempt) {
    const id = sessionId.value;
    if (!id || !isCurrent(revision, id)) return;
    let ended = false;
    const handlers = callbacks(revision, id);
    const finish = (callback: (() => void) | undefined) => { ended = true; callback?.(); };
    try {
      const cancel = await api.chatWithSession(
        id, request?.message ?? '', lastEventId.value, lastEventSeq.value,
        (request?.files ?? []).filter(file => file.file_id && !file.file_id.startsWith('temp-')).map(file => ({ file_id: file.file_id, filename: file.filename })),
        request?.skills ?? [], request?.mcpServers ?? [], request?.agentProfileId ?? null,
        { ...handlers, onClose: () => finish(handlers.onClose), onError: error => { ended = true; handlers.onError?.(error); } },
        request?.datasetIds, continuation?.clientMessageId, continuation?.resumeFrom, request?.inputFileIds,
      );
      if (!isCurrent(revision, id) || ended) cancel();
      else cancelCurrentChat.value = cancel;
    } catch {
      if (!isCurrent(revision, id)) return;
      isLoading.value = false;
      clearAnalysisProgress();
      taskStartedAtMs.value = undefined;
      loadingStatus.value = '';
      cancelCurrentChat.value = null;
      connectionNotice.value = '连接未能建立，请刷新页面确认任务状态。';
    }
  }

  async function send(request: AnalysisSessionRequest) {
    if (disposed || isLoading.value || isRestoringHistory.value || !request.message.trim()) return false;
    request = { ...request,
      files: request.files?.map(file => ({ ...file })),
      skills: request.skills && [...request.skills],
      mcpServers: request.mcpServers && [...request.mcpServers],
      datasetIds: request.datasetIds && [...request.datasetIds],
      inputFileIds: request.inputFileIds && [...request.inputFileIds],
    };
    const revision = invalidate();
    analysisContinuation.value = null;
    startUserTurn();
    messages.value.push({ type: 'user', content: { content: request.message, timestamp: Math.floor(Date.now() / 1000) } as MessageContent });
    if (request.files?.length) messages.value.push({ type: 'attachments', content: { role: 'user', attachments: request.files } as AttachmentsContent });
    isLoading.value = true;
    taskStartedAtMs.value = performance.now();
    connectionNotice.value = '';
    loadingStatus.value = '正在准备分析…';
    if (!sessionId.value) {
      try {
        const created = await api.createSession(request.agentProfileId);
        if (!isCurrent(revision)) return false;
        sessionId.value = created.session_id;
        sessionCreatedAt.value = created.created_at;
        options.onSessionCreated?.(created);
      } catch {
        if (isCurrent(revision)) {
          isLoading.value = false;
          clearAnalysisProgress();
          taskStartedAtMs.value = undefined;
          loadingStatus.value = '';
          connectionNotice.value = '无法创建分析会话，请稍后重试。';
        }
        return false;
      }
    }
    await connect(revision, request);
    return isCurrent(revision);
  }

  function canResumeAnalysis(index: number) {
    return Boolean(!disposed && sessionId.value && !isLoading.value && !isRestoringHistory.value && !cancelCurrentChat.value && resumableAnalysisOutcome(messages.value, index));
  }

  async function resumeAnalysis(index: number) {
    if (!canResumeAnalysis(index) || !sessionId.value) return;
    const outcome = resumableAnalysisOutcome(messages.value, index);
    if (!outcome?.resume_from) return;
    const attempt = continuationAttempt(analysisContinuation.value, sessionId.value, outcome.resume_from, api.createClientMessageId);
    analysisContinuation.value = attempt;
    const revision = invalidate();
    beginAnalysisProgress();
    isLoading.value = true;
    connectionNotice.value = '';
    follow.value = true;
    completionAdvice.value = undefined;
    taskStartedAtMs.value = performance.now();
    loadingStatus.value = '正在继续未完成部分…';
    // No inputs or capabilities from the current composer override the checkpoint.
    await connect(revision, undefined, attempt);
  }

  function reset() {
    invalidate();
    options.onReset?.();
    beginAnalysisProgress();
    resetAgentEventCursor(eventCursor);
    sessionId.value = undefined;
    sessionCreatedAt.value = null;
    messages.value = [];
    isLoading.value = false;
    connectionNotice.value = '';
    loadingStatus.value = '';
    hasMoreHistory.value = false;
    historyBeforeSeq.value = undefined;
    lastEventId.value = undefined;
    lastEventSeq.value = undefined;
    taskStartedAtMs.value = undefined;
    analysisContinuation.value = null;
    plan.value = undefined;
    lastTool.value = undefined;
    lastNoMessageTool.value = undefined;
    completionAdvice.value = undefined;
    selectedSkills.value = [];
    selectedMcpServers.value = [];
    follow.value = true;
    timelineRevision.value = 0;
  }

  async function restore(id: string) {
    if (disposed) return;
    reset();
    sessionId.value = id;
    const revision = generation;
    const request = new AbortController();
    historyRequest = request;
    isRestoringHistory.value = true;
    try {
      const session = await api.getSessionHistory(id, undefined, request.signal);
      if (request.signal.aborted || !isCurrent(revision, id)) return;
      sessionCreatedAt.value = session.created_at;
      hasMoreHistory.value = session.has_more && session.next_before_seq != null;
      historyBeforeSeq.value = session.next_before_seq ?? undefined;
      for (const event of session.events) {
        if (event.event === 'message') {
          const data = event.data as MessageEventData;
          if (data.role === 'user') {
            selectedSkills.value = data.metadata?.skills ?? selectedSkills.value;
            selectedMcpServers.value = data.metadata?.mcp_servers ?? selectedMcpServers.value;
          }
        }
        handleEvent(event);
      }
      options.onSessionRestored?.(session);
      if (session.status === 'running' || session.status === 'pending') {
        isLoading.value = true;
        await connect(revision);
      } else { isLoading.value = false; clearAnalysisProgress(); }
      return session;
    } catch (error) {
      if (!request.signal.aborted && isCurrent(revision, id)) throw error;
    } finally {
      if (isCurrent(revision, id)) isRestoringHistory.value = false;
    }
  }

  async function loadEarlierHistory() {
    const id = sessionId.value;
    const before = historyBeforeSeq.value;
    if (!id || !before || isLoadingHistory.value || isRestoringHistory.value || disposed) return;
    const revision = generation;
    historyRequest?.abort();
    const request = new AbortController();
    historyRequest = request;
    isLoadingHistory.value = true;
    follow.value = false;
    try {
      const page = await api.getSessionHistory(id, before, request.signal);
      if (request.signal.aborted || !isCurrent(revision, id)) return;
      const viewport = options.getViewport?.();
      const oldHeight = viewport?.scrollHeight ?? 0;
      const oldTop = viewport?.scrollTop ?? 0;
      messages.value = prependHistoricalMessages(projectHistoryMessages(page.events, true), messages.value);
      hasMoreHistory.value = page.has_more && page.next_before_seq != null && page.next_before_seq < before;
      historyBeforeSeq.value = hasMoreHistory.value ? page.next_before_seq! : undefined;
      await nextTick();
      if (viewport && isCurrent(revision, id)) viewport.scrollTop = oldTop + viewport.scrollHeight - oldHeight;
    } catch {
      if (!request.signal.aborted && isCurrent(revision, id)) options.onHistoryError?.();
    } finally {
      if (isCurrent(revision, id)) isLoadingHistory.value = false;
    }
  }

  async function stop() {
    if (disposed || stopping) return;
    const id = sessionId.value;
    const revision = invalidate();
    // The stop endpoint targets the session, not an individual turn. Keep the
    // composer locked until it returns so a late stop cannot hit the next task.
    stopping = Boolean(id);
    isLoading.value = Boolean(id);
    loadingStatus.value = id ? '正在停止分析…' : '';
    connectionNotice.value = '';
    clearAnalysisProgress();
    if (!id) return;
    try { await api.stopSession(id); }
    catch {
      if (isCurrent(revision, id)) connectionNotice.value = '停止请求未能确认，请刷新页面检查任务状态。';
      return;
    } finally {
      if (isCurrent(revision, id)) {
        stopping = false;
        isLoading.value = false;
        loadingStatus.value = '';
        taskStartedAtMs.value = undefined;
      }
    }
    if (!isCurrent(revision, id)) return;
    failActiveSteps();
    taskStartedAtMs.value = undefined;
    options.onTerminal?.();
  }

  function dispose() {
    disposed = true;
    invalidate();
    clearAnalysisProgress();
  }

  return { sessionId, sessionCreatedAt, messages, isLoading, connectionNotice, loadingStatus, hasMoreHistory,
    isLoadingHistory, isRestoringHistory, historyBeforeSeq, timelineRevision, follow, plan, lastTool, lastNoMessageTool,
    completionAdvice, selectedSkills, selectedMcpServers, lastEventId, lastEventSeq, taskStartedAtMs, analysisProgress,
    messageKey, send, restore, loadEarlierHistory, stop, reset, dispose, canResumeAnalysis, resumeAnalysis, handleEvent };
}
