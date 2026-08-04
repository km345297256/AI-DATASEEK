import type { FileInfo } from '../api/file';

export const normalizeFilePath = (path: string) => {
  const stack: string[] = [];
  path.replace(/\\/g, '/').split('/').forEach((part) => {
    if (!part || part === '.') return;
    if (part === '..') {
      stack.pop();
      return;
    }
    stack.push(part);
  });
  return stack.join('/');
};

const filePathOf = (file: FileInfo) => String(
  file.file_path || file.metadata?.file_path || file.filename || '',
).replace(/\\/g, '/');

const basename = (path: string) => path.split('/').pop() || path;

const dirname = (path: string) => {
  const normalized = path.replace(/\\/g, '/');
  const index = normalized.lastIndexOf('/');
  return index >= 0 ? normalized.slice(0, index) : '';
};

export const isRelativeResourceUrl = (value: string) => {
  const trimmed = value.trim();
  return Boolean(trimmed)
    && !trimmed.startsWith('#')
    && !/^(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(trimmed)
    && !/^(?:data|blob|mailto|tel|javascript):/i.test(trimmed);
};

export const splitResourceUrl = (value: string) => {
  const hashIndex = value.indexOf('#');
  const queryIndex = value.indexOf('?');
  const indexes = [hashIndex, queryIndex].filter((index) => index >= 0);
  const splitIndex = indexes.length ? Math.min(...indexes) : -1;
  return splitIndex >= 0
    ? { path: value.slice(0, splitIndex), suffix: value.slice(splitIndex) }
    : { path: value, suffix: '' };
};

export const findRelatedFile = (
  currentFile: FileInfo,
  relatedFiles: FileInfo[],
  relativeUrl: string,
) => {
  const { path } = splitResourceUrl(relativeUrl);
  let decodedPath = path;
  try {
    decodedPath = decodeURIComponent(path);
  } catch {
    // Keep malformed percent escapes literal so preview rendering can continue.
  }

  const currentDir = dirname(filePathOf(currentFile));
  const expectedFullPath = normalizeFilePath(
    currentDir ? `${currentDir}/${decodedPath}` : decodedPath,
  ).toLowerCase();
  const expectedBasename = basename(decodedPath).toLowerCase();
  const candidates = relatedFiles.filter(
    (file) => file.file_id && file.file_id !== currentFile.file_id,
  );

  return candidates.find(
    (file) => normalizeFilePath(filePathOf(file)).toLowerCase() === expectedFullPath,
  ) || candidates.find(
    (file) => normalizeFilePath(file.filename || '').toLowerCase()
      === normalizeFilePath(decodedPath).toLowerCase(),
  ) || candidates.find(
    (file) => basename(filePathOf(file)).toLowerCase() === expectedBasename,
  ) || null;
};
