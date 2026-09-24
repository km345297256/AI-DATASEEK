import { nextTick, ref } from 'vue';
import type { FileInfo } from '../api/file';
import type { SSECallbacks } from '../api/client';
import type { AgentSSEEvent, CompletionAdviceData, DoneEventData, ErrorEventData, MessageEventData, PlanEventData, StepEventData, TitleEventData, ToolEventData } from '../types/event';
import type { AttachmentsContent, Message, MessageContent, StepContent, ToolContent } from '../types/message';
import type { AnalysisContinuationAttempt } from '../types/analysisOutcome';
import type { CreateSessionResponse, GetSessionHistoryResponse } from '../types/response';
import { mergeAnalysisToolEvent } from '../utils/analysisJob.ts';
import { AnalysisTimelineIndex } from '../utils/analysisTimelineIndex.ts';
import { completeRunningSteps, failRunningSteps, insertTaskExecutionSummary } from '../utils/chatTimeline.ts';
import { acceptAgentEvent, createAgentEventCursor, resetAgentEventCursor } from '../utils/agentEventCursor.ts';
import { createMessageKey, isLegacyPlanProgressMessage, prependHistoricalMessages, projectHistoryMessages } from '../utils/sessionHistory.ts';
import { isPlaceholderAssistantMessage } from '../utils/datasetResultPresentation.ts';
import { continuationAttempt, resumableAnalysisOutcome } from '../utils/analysisOutcome.ts';
import { isAnalysisProgressEvent, isAnalysisProgressMessage } from '../utils/analysisProgress.ts';
import { useAnalysisProgress } from './useAnalysisProgress.ts';
import { SSEConnectionError } from '../utils/sseConnection.ts';
import { ConversationViewport } from '../utils/conversationViewport.ts';

type SessionApi = Pick<typeof import('../api/agent'), 'chatWithSession' | 'createSession' | 'createClientMessageId' | 'getSessionHistory' | 'stopSession'>
  & Partial<Pick<typeof import('../api/agent'), 'getInputReceipt'>>;

interface PendingInput {
  readonly clientMessageId: string;
  readonly request: AnalysisSessionRequest;
  readonly echoes: Message[];
  readonly timestamp: number;
  state: 'sending' | 'unknown' | 'accepted' | 'rejected';
  uncertain: boolean;
}

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
  /** Restore rejected local input without overwriting a newer composer draft. */
  onInputRejected?: (request: AnalysisSessionRequest) => void;
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
  const canRetryInput = ref(false);
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
  const timelineIndex = new AnalysisTimelineIndex();
  const viewportState = new ConversationViewport(() => follow.value);
  let generation = 0;
  let disposed = false;
  let stopping = false;
  let historyRequest: AbortController | undefined;
  let receiptRequest: AbortController | undefined;
  let pendingInput: PendingInput | undefined;
  let viewportScheduled = false;

  function syncViewport() {
    if (disposed) return;
    viewportState.bind(options.getViewport?.());
    viewportState.layout();
  }
  const isViewportReaderScroll = () => viewportState.readerMoved(options.getViewport?.());

  function scheduleViewport() {
    if (viewportScheduled) return;
    viewportScheduled = true;
    const revision = generation;
    void nextTick(() => {
      viewportScheduled = false;
      if (isCurrent(revision)) syncViewport();
    });
  }

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
    receiptRequest?.abort();
    receiptRequest = undefined;
    isLoadingHistory.value = false;
    isRestoringHistory.value = false;
    cancelTransport();
    viewportState.dispose();
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
    // A replay may expose the durable user event. Hand off by identity, never
    // text, so intentional identical questions remain separate turns.
    if (data.role === 'user' && pendingInput
      && data.metadata?.client_message_id === pendingInput.clientMessageId) {
      Object.assign(pendingInput.echoes[0].content, data);
      if (Array.isArray(data.attachments)) {
        const attachmentEcho = pendingInput.echoes.find(message => message.type === 'attachments');
        if (attachmentEcho && data.attachments.length) {
          Object.assign(attachmentEcho.content, { role: 'user', attachments: data.attachments, timestamp: data.timestamp });
        } else if (attachmentEcho) {
          messages.value = messages.value.filter(message => message !== attachmentEcho);
          pendingInput.echoes.splice(pendingInput.echoes.indexOf(attachmentEcho), 1);
        } else if (data.attachments.length) {
          const position = messages.value.indexOf(pendingInput.echoes[0]) + 1;
          messages.value.splice(position, 0, { type: 'attachments', content: { role: 'user', attachments: data.attachments, timestamp: data.timestamp } as AttachmentsContent });
          pendingInput.echoes.push(messages.value[position]);
        }
      }
      pendingInput.state = 'accepted';
      return;
    }
    if (data.role === 'user') startUserTurn();
    if (data.role === 'assistant' && (isPlaceholderAssistantMessage(data.content) || isLegacyPlanProgressMessage(data.content))) return;
    messages.value.push({ type: data.role, content: { ...data } as MessageContent });
    if (data.attachments?.length) messages.value.push({ type: 'attachments', content: { ...data } as AttachmentsContent });
  }

  function handleTool(data: ToolEventData) {
    let tool = { ...data } as ToolContent;
    const existing = timelineIndex.tool(messages.value, tool.tool_call_id);
    if (existing) {
      Object.assign(existing, mergeAnalysisToolEvent(existing, tool));
      tool = existing;
    } else {
      const step = timelineIndex.runningStep(messages.value);
      if (step) {
        step.tools.push(tool);
        // Keep Vue's observed instance, not the raw object supplied to push().
        tool = step.tools[step.tools.length - 1];
        timelineIndex.addTool(tool);
      }
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
    const existing = timelineIndex.step(messages.value, data.id);
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
    if (!isAnalysisProgressEvent(event)) scheduleViewport();
  }

  function rejectInput(attempt: PendingInput) {
    if (pendingInput !== attempt) return;
    attempt.state = 'rejected';
    const echoes = new Set(attempt.echoes);
    messages.value = messages.value.filter(message => !echoes.has(message));
    pendingInput = undefined;
    canRetryInput.value = false;
    options.onInputRejected?.(attempt.request);
  }

  function callbacks(revision: number, id: string, attempt?: PendingInput): SSECallbacks<AgentSSEEvent['data']> {
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
      onRetry: ({ attempt: count, maxAttempts }) => {
        if (current()) {
          if (attempt) attempt.uncertain = true;
          isLoading.value = true; connectionNotice.value = `连接中断，正在恢复（${count}/${maxAttempts}）…`;
        }
      },
      onMessage: ({ event, data }) => {
        if (!current()) return;
        if (attempt) attempt.uncertain = true;
        handleEvent({ event: event as AgentSSEEvent['event'], data });
        // Old adapters have no receipt endpoint. Production probes durable
        // acceptance before a subsequent send; transport events alone do not
        // identify which input was accepted.
        if (attempt && !api.getInputReceipt && ['done', 'wait', 'error'].includes(event)) {
          if (pendingInput === attempt) pendingInput = undefined;
          canRetryInput.value = false;
        }
      },
      onClose: () => {
        if (current()) {
          close();
          if (attempt?.state !== 'unknown') { canRetryInput.value = false; connectionNotice.value = ''; }
          options.onTerminal?.();
        }
      },
      onError: (error) => {
        if (!current()) return;
        // Transport uncertainty is not a confirmed execution failure.
        close(); connectionNotice.value = error.message; options.onTerminal?.();
        if (attempt && pendingInput === attempt) {
          if (!attempt.uncertain && error instanceof SSEConnectionError && error.kind === 'http'
            && [400, 401, 403, 404, 422].includes(error.status ?? 0)) rejectInput(attempt);
          else {
            attempt.state = 'unknown'; attempt.uncertain = true; canRetryInput.value = true;
            connectionNotice.value = `${error.message} 点击此处恢复原提交。`;
          }
        }
      },
    };
  }

  async function connect(revision: number, request?: AnalysisSessionRequest, continuation?: AnalysisContinuationAttempt, attempt?: PendingInput) {
    const id = sessionId.value;
    if (!id || !isCurrent(revision, id)) return;
    let ended = false;
    const handlers = callbacks(revision, id, attempt);
    const finish = (callback: (() => void) | undefined) => { ended = true; callback?.(); };
    try {
      const cancel = await api.chatWithSession(
        id, request?.message ?? '', lastEventId.value, lastEventSeq.value,
        (request?.files ?? []).filter(file => file.file_id && !file.file_id.startsWith('temp-')).map(file => ({ file_id: file.file_id, filename: file.filename })),
        request?.skills ?? [], request?.mcpServers ?? [], request?.agentProfileId ?? null,
        { ...handlers, onClose: () => finish(handlers.onClose), onError: error => { ended = true; handlers.onError?.(error); } },
        request?.datasetIds, attempt?.clientMessageId ?? continuation?.clientMessageId, continuation?.resumeFrom, request?.inputFileIds, attempt?.timestamp,
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
      if (attempt && pendingInput === attempt) {
        attempt.state = 'unknown'; attempt.uncertain = true; canRetryInput.value = true;
        connectionNotice.value = '连接未能建立，点击此处恢复原提交。';
      }
    }
  }

  async function receipt(attempt: PendingInput, revision: number, id: string) {
    if (!api.getInputReceipt) return undefined;
    const request = new AbortController();
    receiptRequest?.abort(); receiptRequest = request;
    const result = await api.getInputReceipt(id, attempt.clientMessageId, request.signal);
    if (request.signal.aborted || !isCurrent(revision, id) || pendingInput !== attempt) return undefined;
    if (result.client_message_id !== attempt.clientMessageId || typeof result.accepted !== 'boolean') throw new Error('Invalid input receipt');
    if (result.accepted) attempt.state = 'accepted';
    return result;
  }

  /** Explicit recovery of the existing submission, never text-based deduplication. */
  async function retryPendingInput() {
    const attempt = pendingInput;
    const id = sessionId.value;
    if (!attempt || !id || disposed || isLoading.value || isRestoringHistory.value) return false;
    const revision = invalidate();
    isLoading.value = true; canRetryInput.value = false;
    connectionNotice.value = ''; loadingStatus.value = '正在确认并恢复原提交…';
    try {
      const observed = await receipt(attempt, revision, id);
      if (!isCurrent(revision, id) || pendingInput !== attempt) return false;
      attempt.state = observed?.accepted ? 'accepted' : 'sending';
      // An unobserved record can still commit. Retry with the original ID AND
      // all original inputs; an accepted record needs only event replay.
      await connect(revision, observed?.accepted ? undefined : attempt.request, undefined, attempt);
      return isCurrent(revision, id);
    } catch {
      if (isCurrent(revision, id)) {
        isLoading.value = false; loadingStatus.value = ''; canRetryInput.value = true;
        connectionNotice.value = '暂时无法确认原提交，点击此处重试；不会另发一条分析请求。';
      }
      return false;
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
    // A new user action always gets a new identity, but first reconcile an
    // unresolved prior submission instead of silently replacing its ownership.
    if (pendingInput && sessionId.value) {
      const previous = pendingInput, id = sessionId.value, revision = generation;
      const needsReplay = previous.state === 'unknown';
      isRestoringHistory.value = true;
      try {
        const observed = await receipt(previous, revision, id);
        if (!isCurrent(revision, id)) return false;
        if (!observed?.accepted || !['completed', 'cancelled', 'interrupted'].includes(observed.state ?? '')) {
          canRetryInput.value = true;
          connectionNotice.value = '上一条提交尚未确认结束，点击此处恢复；当前草稿已保留。';
          return false;
        }
        if (needsReplay) {
          // The receipt proves acceptance/termination, not delivery of the
          // intervening output. Reattach first so the next turn cannot hide it.
          isLoading.value = true;
          connectionNotice.value = '上一条任务已结束，正在恢复其结果；当前草稿已保留。';
          await connect(revision, undefined, undefined, previous);
          return false;
        }
        pendingInput = undefined; canRetryInput.value = false;
      } catch {
        if (isCurrent(revision, id)) {
          canRetryInput.value = true;
          connectionNotice.value = '暂时无法确认上一条提交，点击此处恢复；当前草稿已保留。';
        }
        return false;
      } finally { if (isCurrent(revision, id)) isRestoringHistory.value = false; }
    }
    const revision = invalidate();
    analysisContinuation.value = null;
    startUserTurn();
    const clientMessageId = api.createClientMessageId();
    const timestamp = Math.floor(Date.now() / 1000);
    messages.value.push({ type: 'user', content: { content: request.message, timestamp, metadata: { client_message_id: clientMessageId } } as MessageContent });
    const echoes = [messages.value[messages.value.length - 1]];
    if (request.files?.length) {
      messages.value.push({ type: 'attachments', content: { role: 'user', attachments: request.files } as AttachmentsContent });
      echoes.push(messages.value[messages.value.length - 1]);
    }
    const attempt: PendingInput = { clientMessageId, request, echoes, timestamp, state: 'sending', uncertain: false };
    pendingInput = attempt; canRetryInput.value = false;
    scheduleViewport();
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
          rejectInput(attempt);
        }
        return false;
      }
    }
    await connect(revision, request, undefined, attempt);
    return isCurrent(revision) && attempt.state !== 'rejected';
  }

  function canResumeAnalysis(index: number) {
    return Boolean(!disposed && sessionId.value && !isLoading.value && !isRestoringHistory.value && !cancelCurrentChat.value
      && messages.value[index]?.type === 'assistant' && timelineIndex.isLastDialogue(messages.value, index)
      && resumableAnalysisOutcome(messages.value, index));
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
    pendingInput = undefined;
    canRetryInput.value = false;
    options.onReset?.();
    beginAnalysisProgress();
    resetAgentEventCursor(eventCursor);
    timelineIndex.clear();
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
      const anchored = viewportState.capture(viewport);
      messages.value = prependHistoricalMessages(projectHistoryMessages(page.events, true), messages.value);
      hasMoreHistory.value = page.has_more && page.next_before_seq != null && page.next_before_seq < before;
      historyBeforeSeq.value = hasMoreHistory.value ? page.next_before_seq! : undefined;
      await nextTick();
      if (viewport && isCurrent(revision, id)) {
        if (anchored) viewportState.layout();
        else viewport.scrollTop = oldTop + viewport.scrollHeight - oldHeight;
      }
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
      if (isCurrent(revision, id)) {
        connectionNotice.value = '停止请求未能确认，请刷新页面检查任务状态。';
        if (pendingInput) {
          pendingInput.state = 'unknown'; pendingInput.uncertain = true; canRetryInput.value = true;
          connectionNotice.value = '停止请求未能确认，点击此处恢复原提交并检查任务状态。';
        }
      }
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
    pendingInput = undefined;
    canRetryInput.value = false;
    failActiveSteps();
    taskStartedAtMs.value = undefined;
    options.onTerminal?.();
  }

  function dispose() {
    disposed = true;
    invalidate();
    pendingInput = undefined;
    canRetryInput.value = false;
    timelineIndex.clear();
    clearAnalysisProgress();
  }

  return { sessionId, sessionCreatedAt, messages, isLoading, connectionNotice, canRetryInput, retryPendingInput, syncViewport, isViewportReaderScroll, loadingStatus, hasMoreHistory,
    isLoadingHistory, isRestoringHistory, historyBeforeSeq, timelineRevision, follow, plan, lastTool, lastNoMessageTool,
    completionAdvice, selectedSkills, selectedMcpServers, lastEventId, lastEventSeq, taskStartedAtMs, analysisProgress,
    messageKey, send, restore, loadEarlierHistory, stop, reset, dispose, canResumeAnalysis, resumeAnalysis, handleEvent };
}
