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
