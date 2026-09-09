/** Abortable bounded transfer. Content-Length is a hint, not the enforcement. */
export async function readBoundedBinary(response: Response, maxBytes: number, signal: AbortSignal): Promise<ArrayBuffer> {
  const declared = Number(response.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > maxBytes) {
    await response.body?.cancel().catch(() => undefined);
    throw new Error('文件超过安全预览读取上限，请下载后分析。');
  }
  if (!response.body) throw new Error('浏览器不支持有界流式读取，无法安全预览此文件。');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  const abort = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener('abort', abort, { once: true });
  try {
    while (true) {
      if (signal.aborted) throw new DOMException('Preview cancelled', 'AbortError');
      const part = await reader.read();
      if (signal.aborted) throw new DOMException('Preview cancelled', 'AbortError');
      if (part.done) break;
      length += part.value.byteLength;
      if (length > maxBytes) throw new Error('文件超过安全预览读取上限，请下载后分析。');
      chunks.push(part.value);
    }
    const buffer = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) { buffer.set(chunk, offset); offset += chunk.byteLength; }
    return buffer.buffer;
  } finally {
    signal.removeEventListener('abort', abort);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export function validateTiffDimensions(ifd: { t256?: number[]; t257?: number[]; t258?: number[]; t277?: number[] }): { width: number; height: number } {
  const width = ifd.t256?.[0] ?? 0, height = ifd.t257?.[0] ?? 0;
  const channels = Math.max(ifd.t258?.length ?? 1, ifd.t277?.[0] ?? 1);
  const bits = ifd.t258 && ifd.t258.length <= 8 ? Math.max(...ifd.t258) : ifd.t258 ? Infinity : 8;
  if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width < 1 || height < 1 || width * height > 16 * 1024 * 1024
    || !Number.isSafeInteger(channels) || channels < 1 || channels > 8 || !Number.isFinite(bits) || bits < 1 || bits > 64
    || width * height * channels * bits / 8 > 128 * 1024 * 1024) {
    throw new Error('TIFF 解码超过安全上限（16 百万像素 / 128 MiB 原始数据），请下载后分析。');
  }
  return { width, height };
}
