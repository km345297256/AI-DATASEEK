/** First-party media guard. No playlists, external resources or decoder plug-ins. */
export const AUDIO_BYTES = 16 * 1024 * 1024;
export const VIDEO_BYTES = 64 * 1024 * 1024;
const formats: Record<string, { kind: 'audio' | 'video'; mime: string }> = {
  mp4: { kind: 'video', mime: 'video/mp4' }, m4v: { kind: 'video', mime: 'video/mp4' },
  mov: { kind: 'video', mime: 'video/quicktime' }, webm: { kind: 'video', mime: 'video/webm' },
  ogv: { kind: 'video', mime: 'video/ogg' }, wav: { kind: 'audio', mime: 'audio/wav' },
  mp3: { kind: 'audio', mime: 'audio/mpeg' }, m4a: { kind: 'audio', mime: 'audio/mp4' },
  ogg: { kind: 'audio', mime: 'audio/ogg' }, flac: { kind: 'audio', mime: 'audio/flac' },
};
export function mediaFormat(filename: string, mode: 'audio' | 'video') {
  const extension = filename.split('.').pop()?.toLowerCase() ?? '';
  const format = formats[extension];
  if (!format || format.kind !== mode) throw new Error('此插件仅支持批准的本地媒体格式，不打开播放列表或外部链接。');
  return { ...format, extension, maxBytes: mode === 'audio' ? AUDIO_BYTES : VIDEO_BYTES };
}
export interface Waveform {
  channels: number; sampleRate: number; duration: number; frames: number;
  peaks: Array<Array<{ min: number; max: number }>>;
}
const tag = (view: DataView, start: number) => String.fromCharCode(...Array.from({ length: 4 }, (_, index) => view.getUint8(start + index)));
/** Only uncompressed, interleaved PCM/float RIFF WAV; allocate at most 4096 buckets. */
export function pcmWaveform(buffer: ArrayBuffer): Waveform {
  const fail = () => new Error('波形只支持有界的未压缩 PCM/float WAV（≤120秒、双声道、4百万采样值）；其他音频可尝试直接播放。');
  if (buffer.byteLength < 44 || buffer.byteLength > AUDIO_BYTES) throw fail();
  const view = new DataView(buffer);
  if (tag(view, 0) !== 'RIFF' || tag(view, 8) !== 'WAVE' || view.getUint32(4, true) + 8 !== buffer.byteLength) throw fail();
  let offset = 12, chunks = 0;
  let format: { code: number; channels: number; sampleRate: number; block: number; bits: number } | undefined;
  let data: { start: number; size: number } | undefined;
  while (offset < buffer.byteLength) {
    if (++chunks > 256 || offset + 8 > buffer.byteLength) throw fail();
    const name = tag(view, offset), size = view.getUint32(offset + 4, true), start = offset + 8;
    if (size > buffer.byteLength - start) throw fail();
    if (name === 'fmt ') {
      if (format || size < 16 || size > 40) throw fail();
      format = { code: view.getUint16(start, true), channels: view.getUint16(start + 2, true),
        sampleRate: view.getUint32(start + 4, true), block: view.getUint16(start + 12, true), bits: view.getUint16(start + 14, true) };
      if (![1, 3].includes(format.code) || ![1, 2].includes(format.channels)
        || format.sampleRate < 1 || format.sampleRate > 192000
        || !(format.code === 1 ? [8, 16, 24, 32].includes(format.bits) : format.bits === 32)
        || format.block !== format.channels * format.bits / 8
        || view.getUint32(start + 8, true) !== format.sampleRate * format.block) throw fail();
    } else if (name === 'data') {
      if (data) throw fail();
      data = { start, size };
    }
    offset = start + size + (size % 2);
    if (offset > buffer.byteLength) throw fail();
  }
  if (!format || !data || !data.size || data.size % format.block) throw fail();
  const frames = data.size / format.block, duration = frames / format.sampleRate;
  if (duration > 120 || frames * format.channels > 4000000) throw fail();
  const count = Math.min(2048, frames), peaks: Waveform['peaks'] = [];
  const read = (position: number) => {
    if (format!.code === 3) return view.getFloat32(position, true);
    if (format!.bits === 8) return (view.getUint8(position) - 128) / 128;
    if (format!.bits === 16) return view.getInt16(position, true) / 32768;
    if (format!.bits === 32) return view.getInt32(position, true) / 2147483648;
    const unsigned = view.getUint8(position) | view.getUint8(position + 1) << 8 | view.getUint8(position + 2) << 16;
    return ((unsigned << 8) >> 8) / 8388608;
  };
  for (let channel = 0; channel < format.channels; channel++) {
    const bins: Waveform['peaks'][number] = [];
    for (let bin = 0; bin < count; bin++) {
      const first = Math.floor(bin * frames / count), end = Math.floor((bin + 1) * frames / count);
      let min = Infinity, max = -Infinity;
      for (let frame = first; frame < end; frame++) {
        const value = read(data.start + frame * format.block + channel * format.bits / 8);
        if (!Number.isFinite(value)) throw fail();
        min = Math.min(min, value); max = Math.max(max, value);
      }
      bins.push({ min, max });
    }
    peaks.push(bins);
  }
  return { channels: format.channels, sampleRate: format.sampleRate, duration, frames, peaks };
}
