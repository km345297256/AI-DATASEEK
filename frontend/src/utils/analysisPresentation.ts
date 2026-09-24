import type { FileInfo } from '../api/file';
import type { AttachmentsContent, Message } from '../types/message';

/** Presentation only: input files precede their question, without changing the
 * timeline that execution, history cursors and resume actions use. A completed
 * turn may have its task summary inserted between the question and its files.
 */
export function conversationEntries(messages: Message[]): { message: Message; index: number }[] {
  const entries = messages.map((message, index) => ({ message, index }));
  const ordered: typeof entries = [];
  for (let start = 0; start < entries.length;) {
    const entry = entries[start]!;
    if (entry.message.type !== 'user') {
      ordered.push(entry);
      start += 1;
      continue;
    }
    let end = start + 1;
    while (end < entries.length && entries[end]!.message.type !== 'user') end += 1;
    const turn = entries.slice(start + 1, end);
    const isInput = ({ message }: (typeof entries)[number]) => message.type === 'attachments'
      && (message.content as AttachmentsContent).role === 'user';
    ordered.push(...turn.filter(isInput), entry, ...turn.filter(item => !isInput(item)));
    start = end;
  }
  return ordered;
}

/** Only published assistant attachments are deliverables, never user inputs. */
export function deliveredFiles(message: Message): FileInfo[] {
  if (message.type !== 'attachments' || (message.content as AttachmentsContent).role !== 'assistant') return [];
  return (message.content as AttachmentsContent).attachments || [];
}

export function deliveredImages(message: Message): FileInfo[] {
  return deliveredFiles(message).filter(file => /\.(png|jpe?g|gif|webp|svg)$/i.test(file.filename));
}

export function currentTurnDeliveries(messages: Message[]): FileInfo[] {
  const files = new Map<string, FileInfo>();
  for (const message of messages) {
    if (message.type === 'user') files.clear();
    for (const file of deliveredFiles(message)) files.set(file.file_id, file);
  }
  return [...files.values()];
}

export function precedingTaskSummary(messages: Message[], stepIndex: number): Message | undefined {
  for (let index = stepIndex - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.type === 'user') return undefined;
    if (message.type === 'task-summary') return message;
  }
  return undefined;
}

export interface ConversationEntry { message: Message; index: number; summary?: Message }

/** Preserve completed turn entries while appending to/rebuilding only the active turn.
 * The outer array is replaced for Vue publication; stable entries/message keys keep
 * mounted cards intact. Loading an older page is a single linear pass, not a scan
 * per card. This deliberately does not virtualize or change presentation policy.
 */
export class ConversationPresentationIndex {
  private source?: Message[];
  private length = 0;
  private tail?: Message;
  private entries: ConversationEntry[] = [];
  private currentUser?: Message;
  private turnStart = 0;
  private entryStart = 0;
  private inputCount = 0;
  private summary?: Message;
  private summaryIndex = -1;
  private readonly summaries = new WeakMap<Message, Message>();
  private entryCache = new WeakMap<Message, ConversationEntry>();
  latestAssistant?: Message;
  private assistantBeforeTurn?: Message;

  update(messages: Message[]): ConversationEntry[] {
    const sameStructure = (!this.currentUser || messages[this.turnStart] === this.currentUser)
      && (!this.summary || messages[this.summaryIndex] === this.summary);
    if (messages === this.source && sameStructure && messages.length === this.length && messages[messages.length - 1] === this.tail) return this.entries;
    let start = this.length;
    if (messages !== this.source || !sameStructure || messages.length < this.length
      || (this.length > 0 && messages[this.length - 1] !== this.tail)) {
      // A task summary is inserted inside the active turn at completion.
      const retainHead = messages === this.source && this.currentUser !== undefined
        && messages[this.turnStart] === this.currentUser;
      start = retainHead ? this.turnStart : 0;
      this.entries = retainHead ? this.entries.slice(0, this.entryStart) : [];
      this.currentUser = undefined; this.inputCount = 0; this.summary = undefined; this.summaryIndex = -1;
      this.latestAssistant = retainHead ? this.assistantBeforeTurn : undefined;
    } else this.entries = [...this.entries];
    for (let index = start; index < messages.length; index += 1) {
      const message = messages[index];
      let entry = this.entryCache.get(message);
      if (!entry) { entry = { message, index }; this.entryCache.set(message, entry); }
      entry.index = index;
      if (message.type === 'user') {
        this.assistantBeforeTurn = this.latestAssistant;
        this.currentUser = message; this.turnStart = index; this.entryStart = this.entries.length;
        this.inputCount = 0; this.summary = undefined; this.summaryIndex = -1;
      }
      entry.summary = this.summary;
      if (this.summary) this.summaries.set(message, this.summary); else this.summaries.delete(message);
      if (message.type === 'task-summary') { this.summary = message; this.summaryIndex = index; }
      if (message.type === 'assistant') this.latestAssistant = message;
      if (this.currentUser && message.type === 'attachments' && (message.content as AttachmentsContent).role === 'user') {
        this.entries.splice(this.entryStart + this.inputCount++, 0, entry);
      } else this.entries.push(entry);
    }
    this.source = messages; this.length = messages.length; this.tail = messages[messages.length - 1];
    return this.entries;
  }

  summaryFor(message: Message) { return this.summaries.get(message); }
}
