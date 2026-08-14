import type { ToolContent } from '../types/message';

const REDACTED_SECRET = '[敏感参数已隐藏]';
const REDACTED_SECRET_SENTINEL = '__AI_DATASEEK_REDACTED_SECRET__';
const REDACTED_PATH = '[受保护路径]';
const PREVIEW_LENGTH = 180;

const TOOLKIT_NAMES = new Set([
  'shell',
  'file',
  'browser',
  'search',
  'info',
  'message',
  'mcp',
  'skill',
  'scientific',
]);

const SECRET_KEY_PATTERN = [
  'api[-_]?key',
  'access[-_]?key',
  'secret(?:[-_]?key)?',
  'client[-_]?secret',
  'password',
  'passwd',
  'token',
  'credential',
  'authorization',
  'cookie',
  'private[-_]?key',
  'signature',
].join('|');
const SECRET_IDENTIFIER_PATTERN = `(?:[A-Za-z][A-Za-z0-9]*[-_])*(?:${SECRET_KEY_PATTERN})`;

const isRecord = (value: unknown): value is Record<string, unknown> => (
  !!value && typeof value === 'object' && !Array.isArray(value)
);

const isSensitiveKey = (key: string): boolean => new RegExp(
  `(?:^|[-_])(?:${SECRET_KEY_PATTERN})$`,
  'i',
).test(key.replace(/^--?/, ''));

/**
 * Remove credentials and host-only paths before rendering tool arguments.
 * Sandbox-visible paths remain useful to users; host roots never need to be
 * exposed in a chat timeline or tool panel.
 */
export const sanitizeToolDisplayText = (value: unknown): string => {
  let text = String(value ?? '');

  // Credentials embedded in URLs, HTTP headers, environment assignments,
  // JSON-like values, command flags, and query strings.
  text = text.replace(
    /\b([a-z][a-z0-9+.-]*:\/\/)([^\s/@:'"]+):([^\s/@'"]+)@/gi,
    `$1${REDACTED_SECRET_SENTINEL}@`,
  );
  text = text.replace(
    /\b(Authorization\s*:\s*)(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi,
    `$1$2 ${REDACTED_SECRET_SENTINEL}`,
  );
  text = text.replace(
    /\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi,
    `$1 ${REDACTED_SECRET_SENTINEL}`,
  );
  text = text.replace(
    new RegExp(`(\\b${SECRET_IDENTIFIER_PATTERN}\\s*=\\s*)(?:"[^"]*"|'[^']*'|[^\\s;&|]+)`, 'gi'),
    `$1${REDACTED_SECRET_SENTINEL}`,
  );
  text = text.replace(
    new RegExp(`(["']?${SECRET_IDENTIFIER_PATTERN}["']?\\s*:\\s*)(?:"[^"]*"|'[^']*'|[^\\s,}\\]]+)`, 'gi'),
    `$1${REDACTED_SECRET_SENTINEL}`,
  );
  text = text.replace(
    new RegExp(`(--${SECRET_IDENTIFIER_PATTERN}(?:=|\\s+))(?:"[^"]*"|'[^']*'|[^\\s;&|]+)`, 'gi'),
    `$1${REDACTED_SECRET_SENTINEL}`,
  );
  text = text.replace(
    new RegExp(`((?:[?&])${SECRET_IDENTIFIER_PATTERN}=)[^&#\\s]*`, 'gi'),
    `$1${REDACTED_SECRET_SENTINEL}`,
  );
  text = text.replace(
    /((?:^|\s)(?:-u|--user)\s+)(?:"[^"]*"|'[^']*'|[^\s;&|]+)/gi,
    `$1${REDACTED_SECRET_SENTINEL}`,
  );

  // The analysis sandbox has stable public roots under /home/ubuntu. Other
  // common host dataset roots are intentionally collapsed at the UI boundary.
  text = text.replace(
    /(^|[\s"'`=:(])\/(?:root|data(?:\d+)?|mnt|srv|storage|volume)(?:\/[^\s"'`|;&<>)]*)*/g,
    `$1${REDACTED_PATH}`,
  );
  text = text.replace(
    /(^|[\s"'`=:(])\/opt\/datasets(?:\/[^\s"'`|;&<>)]*)*/g,
    `$1${REDACTED_PATH}`,
  );
  text = text.replace(
    /(^|[\s"'`=:(])\/home\/(?!ubuntu(?:\/|\b))[^\s"'`|;&<>)]*/g,
    `$1${REDACTED_PATH}`,
  );
  text = text.replace(
    /(^|[\s"'`=:(])\/Users\/[^\s"'`|;&<>)]*/g,
    `$1${REDACTED_PATH}`,
  );
  text = text.replace(
    /(^|[\s"'`=:(])(?:[A-Za-z]:\\|\\\\)[^\s"'`|;&<>)]*/g,
    `$1${REDACTED_PATH}`,
  );

  text = text.replace(
    new RegExp(`${REDACTED_SECRET_SENTINEL}(?:\\s+${REDACTED_SECRET_SENTINEL})+`, 'g'),
    REDACTED_SECRET_SENTINEL,
  );
  return text.split(REDACTED_SECRET_SENTINEL).join(REDACTED_SECRET);
};

export const sanitizeToolDisplayValue = (value: unknown, depth = 0): unknown => {
  if (typeof value === 'string') return sanitizeToolDisplayText(value);
  if (value === null || value === undefined || typeof value !== 'object') return value;
  if (depth >= 8) return '[内容过深，已省略]';
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeToolDisplayValue(item, depth + 1));
  }

  return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, item]) => [
    key,
    isSensitiveKey(key) ? REDACTED_SECRET : sanitizeToolDisplayValue(item, depth + 1),
  ]));
};

export const resolveToolFunction = (tool: Pick<ToolContent, 'name' | 'function'>): string => {
  if (tool.function) return tool.function;
  // Older stored events used the function name in `name` and omitted
  // `function`. Keep those timelines readable after an upgrade.
  if (tool.name.includes('_') || !TOOLKIT_NAMES.has(tool.name)) return tool.name;
  return '';
};

export const resolveToolName = (tool: Pick<ToolContent, 'name' | 'function'>): string => {
  if (TOOLKIT_NAMES.has(tool.name)) return tool.name;
  const functionName = resolveToolFunction(tool);
  if (functionName.startsWith('shell_') || functionName.startsWith('dataset_')) return 'shell';
  if (functionName.startsWith('file_')) return 'file';
  if (functionName.startsWith('browser_')) return 'browser';
  if (functionName.startsWith('info_')) return 'info';
  if (functionName.startsWith('message_')) return 'message';
  if (functionName.startsWith('mcp_')) return 'mcp';
  if (functionName.startsWith('skill_')) return 'skill';
  if (functionName.startsWith('scientific_') || functionName.startsWith('geoscience_')) return 'scientific';
  return tool.name || 'tool';
};

const safeStringArg = (args: Record<string, unknown>, key: string): string => {
  const value = args[key];
  if (value === null || value === undefined) return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return sanitizeToolDisplayText(value);
  }
  return sanitizeToolDisplayText(JSON.stringify(sanitizeToolDisplayValue(value)));
};

const displaySandboxPath = (value: string): string => sanitizeToolDisplayText(value)
  .replace(/^\/home\/ubuntu\/?/, '~/');

const joinPathAction = (source: string, destination: string): string => {
  if (source && destination) return `${displaySandboxPath(source)} → ${displaySandboxPath(destination)}`;
  return displaySandboxPath(source || destination);
};

export type ToolDisplayDetail = {
  full: string;
  preview: string;
  expandable: boolean;
};

const toPreview = (full: string): ToolDisplayDetail => {
  const previewSource = full.replace(/\s+/g, ' ').trim();
  const expandable = previewSource.length > PREVIEW_LENGTH || full.includes('\n');
  return {
    full,
    preview: previewSource.length > PREVIEW_LENGTH
      ? `${previewSource.slice(0, PREVIEW_LENGTH - 1).trimEnd()}…`
      : previewSource,
    expandable,
  };
};

export const getToolDisplayDetail = (tool: ToolContent): ToolDisplayDetail => {
  const functionName = resolveToolFunction(tool);
  const args = isRecord(tool.args) ? tool.args : {};
  let detail = '';

  if (functionName === 'shell_exec' || functionName === 'shell_run' || functionName === 'dataset_analysis_run') {
    detail = safeStringArg(args, 'command');
  } else if (functionName === 'shell_wait') {
    const session = safeStringArg(args, 'id') || safeStringArg(args, 'shell');
    const seconds = safeStringArg(args, 'seconds');
    detail = [session, seconds ? `${seconds} 秒` : ''].filter(Boolean).join(' · ');
  } else if (functionName === 'shell_view' || functionName === 'shell_kill_process') {
    detail = safeStringArg(args, 'id') || safeStringArg(args, 'shell');
  } else if (functionName === 'dataset_unpack') {
    detail = joinPathAction(safeStringArg(args, 'archive_path'), safeStringArg(args, 'output_dir'));
  } else if (functionName === 'dataset_quicklook') {
    detail = joinPathAction(safeStringArg(args, 'input_path'), safeStringArg(args, 'output_dir'));
  } else if (functionName === 'dataset_inventory') {
    detail = displaySandboxPath(safeStringArg(args, 'input_path'));
  } else {
    const preferredKeys: Record<string, string> = {
      file_read: 'file',
      file_write: 'file',
      file_str_replace: 'file',
      file_find_in_content: 'file',
      file_find_by_name: 'path',
      browser_navigate: 'url',
      browser_restart: 'url',
      browser_console_exec: 'code',
      info_search_web: 'query',
      message_notify_user: 'message',
      message_ask_user: 'question',
      skill_read: 'name',
    };
    const preferredKey = preferredKeys[functionName];
    if (preferredKey) detail = safeStringArg(args, preferredKey);
    if (!detail) {
      const firstSafeEntry = Object.entries(args).find(([key]) => !isSensitiveKey(key));
      if (firstSafeEntry) detail = safeStringArg(args, firstSafeEntry[0]);
    }
  }

  return toPreview(detail);
};

/** A display-only clone used by the tool panel; the live event is untouched. */
export const safeToolContentForDisplay = (tool: ToolContent): ToolContent => {
  const name = resolveToolName(tool);
  const functionName = resolveToolFunction(tool);
  const sanitizedArgs = sanitizeToolDisplayValue(tool.args);
  const isShellTool = name === 'shell';
  return {
    ...tool,
    name,
    function: functionName,
    args: isRecord(sanitizedArgs) ? sanitizedArgs : {},
    content: isShellTool ? sanitizeToolDisplayValue(tool.content) : tool.content,
  };
};
