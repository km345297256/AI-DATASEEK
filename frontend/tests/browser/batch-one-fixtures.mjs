/** Synthetic-only real-browser coverage. No user files, production API or listener. */
import assert from 'node:assert/strict';
import JSZip from 'jszip';

const encode = text => Buffer.from(text, 'utf8');
const bytesReply = (name, bytes) => ({ contentType: 'application/octet-stream', body: Buffer.from(bytes), headers: {
  'X-Preview-Version': '1'.repeat(64), 'X-Visualization-Revision': '2'.repeat(64), 'X-Visualization-Plugin': `test-${name}`,
} });
const previewReply = (name, kind, payload, metadata = {}) => ({ contentType: 'application/json', body: JSON.stringify({ code: 0, data: {
  contract_version: 2, plugin_id: `test-${name}`, version: '1'.repeat(64), revision: '2'.repeat(64), kind, payload, metadata, warnings: [], sampled: false,
} }) });
const exactRequest = (request, name, operation) => {
  assert.equal(request.method(), 'POST');
  assert.equal(new URL(request.url()).pathname, `/api/v1/files/synthetic-${name}/visualization`);
  const data = request.postDataJSON(); assert.equal(data.plugin_id, `test-${name}`); assert.equal(data.operation, operation); return data;
};

function wavBytes() {
  const rate = 16000, frames = rate * 2, channels = 2, buffer = Buffer.alloc(44 + frames * channels * 2);
  buffer.write('RIFF', 0); buffer.writeUInt32LE(buffer.length - 8, 4); buffer.write('WAVEfmt ', 8); buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(channels, 22); buffer.writeUInt32LE(rate, 24); buffer.writeUInt32LE(rate * channels * 2, 28);
  buffer.writeUInt16LE(channels * 2, 32); buffer.writeUInt16LE(16, 34); buffer.write('data', 36); buffer.writeUInt32LE(frames * channels * 2, 40);
  for (let frame = 0; frame < frames; frame++) {
    buffer.writeInt16LE(Math.round(Math.sin(frame / rate * 2 * Math.PI * 220) * 12000), 44 + frame * 4);
    buffer.writeInt16LE(Math.round(Math.sin(frame / rate * 2 * Math.PI * 440) * 5000), 46 + frame * 4);
  }
  return buffer;
}

/** Baseline two-sample, interleaved unsigned-16 GeoTIFF with one NoData pixel. */
function multibandTiff() {
  const width = 32, height = 32, tagCount = 15, ifdEnd = 8 + 2 + tagCount * 12 + 4;
  const scale = ifdEnd, tie = scale + 24, keys = tie + 48, nodata = keys + 32, pixels = nodata + 6;
  const data = Buffer.alloc(pixels + width * height * 4);
  data.write('II'); data.writeUInt16LE(42, 2); data.writeUInt32LE(8, 4);
  const tags = [[256, 4, 1, width], [257, 4, 1, height], [258, 3, 2, 16 | 16 << 16], [259, 3, 1, 1], [262, 3, 1, 1],
    [273, 4, 1, pixels], [277, 3, 1, 2], [278, 4, 1, height], [279, 4, 1, width * height * 4], [284, 3, 1, 1],
    [339, 3, 2, 1 | 1 << 16], [33550, 12, 3, scale], [33922, 12, 6, tie], [34735, 3, 16, keys], [42113, 2, 6, nodata]];
  data.writeUInt16LE(tags.length, 8);
  tags.forEach(([tag, type, count, value], index) => { const start = 10 + index * 12; data.writeUInt16LE(tag, start); data.writeUInt16LE(type, start + 2); data.writeUInt32LE(count, start + 4); data.writeUInt32LE(value, start + 8); });
  [0.1, 0.1, 0].forEach((value, i) => data.writeDoubleLE(value, scale + i * 8));
  [0, 0, 0, 10, 20, 0].forEach((value, i) => data.writeDoubleLE(value, tie + i * 8));
  [1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326].forEach((value, i) => data.writeUInt16LE(value, keys + i * 2));
  data.write('65535\0', nodata);
  for (let index = 0; index < width * height; index++) {
    data.writeUInt16LE(index === 0 ? 65535 : index % 100, pixels + index * 4);
    data.writeUInt16LE(1000 + (1023 - index) % 200, pixels + index * 4 + 2);
  }
  return data;
}

// MediaRecorder emits a valid stream with unknown duration. Add the known 2-second
// synthetic duration to its EBML Info; do not spoof HTMLMediaElement or its decoder.
function webmDuration(input, milliseconds) {
  const data = Buffer.from(input);
  function vint(start, identifier = false) {
    const first = data[start]; if (!first) throw new Error('Invalid EBML integer');
    let width = 1; while (width <= 8 && !(first & 1 << (8 - width))) width++;
    if (width > 8 || start + width > data.length) throw new Error('Truncated EBML integer');
    let value = identifier ? first : first & (1 << (8 - width)) - 1;
    let unknown = !identifier && value === (1 << (8 - width)) - 1;
    for (let i = 1; i < width; i++) { value = value * 256 + data[start + i]; unknown &&= data[start + i] === 255; }
    if (!unknown && !Number.isSafeInteger(value)) throw new Error('Unsafe EBML size');
    return { value, width, unknown };
  }
  function elements(start, end) {
    const values = [];
    while (start < end) {
      const id = vint(start, true), size = vint(start + id.width), payload = start + id.width + size.width;
      const next = size.unknown ? end : payload + size.value;
      if (next > end || next <= start) throw new Error('Invalid EBML bounds');
      values.push({ id: id.value, start, idWidth: id.width, size, payload, end: next }); start = next;
    }
    return values;
  }
  function sizeBytes(value, width) {
    const out = Buffer.alloc(width); let number = BigInt(value);
    if (number >= (1n << BigInt(width * 7)) - 1n) throw new Error('Fixture EBML metadata needs a larger size field');
    for (let i = width - 1; i >= 0; i--) { out[i] = Number(number & 255n); number >>= 8n; }
    out[0] |= 1 << (8 - width); return out;
  }
  const segment = elements(0, data.length).find(item => item.id === 0x18538067);
  if (!segment) throw new Error('MediaRecorder did not return WebM');
  const info = elements(segment.payload, segment.end).find(item => item.id === 0x1549a966);
  if (!info) throw new Error('WebM Info missing');
  const fields = elements(info.payload, info.end), scaleField = fields.find(item => item.id === 0x2ad7b1);
  let timecodeScale = 1000000;
  if (scaleField) { timecodeScale = 0; for (let i = scaleField.payload; i < scaleField.end; i++) timecodeScale = timecodeScale * 256 + data[i]; }
  const duration = Buffer.alloc(11); duration[0] = 0x44; duration[1] = 0x89; duration[2] = 0x88; duration.writeDoubleBE(milliseconds * 1000000 / timecodeScale, 3);
  const previous = fields.find(item => item.id === 0x4489);
  const payload = Buffer.concat([...fields.filter(item => item !== previous).map(item => data.subarray(item.start, item.end)), duration]);
  const replacement = Buffer.concat([data.subarray(info.start, info.start + info.idWidth), sizeBytes(payload.length, info.size.width), payload]);
  const output = Buffer.concat([data.subarray(0, info.start), replacement, data.subarray(info.end)]);
  if (!segment.size.unknown) sizeBytes(segment.size.value + replacement.length - (info.end - info.start), segment.size.width).copy(output, segment.start + segment.idWidth);
  return output;
}

async function createSyntheticVideo(page) {
  const bytes = await page.evaluate(async () => {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported('video/webm;codecs=vp8')) throw new Error('This Chrome cannot encode the synthetic VP8 WebM fixture');
    const canvas = document.createElement('canvas'); canvas.width = 128; canvas.height = 96;
    const context = canvas.getContext('2d'), stream = canvas.captureStream(10), chunks = [];
    const recorder = new MediaRecorder(stream, { mimeType: 'video/webm;codecs=vp8', videoBitsPerSecond: 100000 });
    const complete = new Promise((resolve, reject) => { recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); }; recorder.onerror = event => reject(new Error(event.error?.message || 'Synthetic encoding failed')); recorder.onstop = resolve; });
    try {
      recorder.start();
      for (let frame = 0; frame < 20; frame++) {
        context.fillStyle = frame < 10 ? '#db4437' : '#4285f4'; context.fillRect(0, 0, 128, 96);
        context.fillStyle = '#ffffff'; context.fillRect(frame * 5 % 100, 24, 20, 30);
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      recorder.stop(); await complete;
      return Array.from(new Uint8Array(await new Blob(chunks, { type: recorder.mimeType }).arrayBuffer()));
    } finally { stream.getTracks().forEach(track => track.stop()); canvas.width = 0; canvas.height = 0; }
  });
  return webmDuration(bytes, 2000);
}

const mediaReady = selector => page => page.waitForFunction(selector => {
  const media = document.querySelector(selector); return media?.readyState >= 2 && Number.isFinite(media.duration) && media.duration > 0 && !media.error;
}, selector, { timeout: 20000 });
const keepMedia = selector => page => page.evaluate(async selector => { window.__heldMedia = document.querySelector(selector); await window.__heldMedia.play(); }, selector);
const mediaCleanup = async page => {
  const initialState = await page.evaluate(() => ({ paused: window.__heldMedia.paused, networkState: window.__heldMedia.networkState, readyState: window.__heldMedia.readyState }));
  const started = Date.now();
  // load() tears down the native decoder asynchronously. A fixed delay in the
  // shared harness is not a completion signal when the full suite is busy.
  // Observe the SAME complete predicate asserted below; do not call pause(),
  // load(), change src, or otherwise help the production cleanup pass.
  let settled = false;
  try {
    await page.waitForFunction(() => {
      const media = window.__heldMedia;
      return media.paused && !media.getAttribute('src') && media.networkState === 0 && media.readyState === 0
        && !Number.isFinite(media.duration) && media.buffered.length === 0 && media.currentTime === 0
        && (media.videoWidth === undefined || media.videoWidth === 0) && (media.videoHeight === undefined || media.videoHeight === 0);
    }, null, { timeout: 3000, polling: 25 });
    settled = true;
  } catch (error) {
    if (error.name !== 'TimeoutError') throw error;
    // Retain the exact native fields in the final assertion on timeout.
  }
  const value = await page.evaluate(() => ({ paused: window.__heldMedia.paused, sourceRemoved: !window.__heldMedia.getAttribute('src'), currentSourceEmpty: window.__heldMedia.currentSrc === '',
    networkState: window.__heldMedia.networkState, readyState: window.__heldMedia.readyState, durationReset: !Number.isFinite(window.__heldMedia.duration), bufferedRanges: window.__heldMedia.buffered.length,
    currentTime: window.__heldMedia.currentTime, videoWidth: window.__heldMedia.videoWidth ?? null, videoHeight: window.__heldMedia.videoHeight ?? null,
  }));
  // Chrome can retain currentSrc as a historical blob string after load() on a
  // detached node. Verify actual native decoder/network/buffer reset instead;
  // keep the historical-string observation in the report, never overwrite it.
  assert.ok(settled && value.paused && value.sourceRemoved && value.networkState === 0 && value.readyState === 0
    && value.durationReset && value.bufferedRanges === 0 && value.currentTime === 0
    && (value.videoWidth === null || value.videoWidth === 0) && (value.videoHeight === null || value.videoHeight === 0), `Native media cleanup: ${JSON.stringify(value)}`);
  return { ...value, initialState, settledWithinMs: Date.now() - started };
};
async function realPlayback(page, selector) {
  assert.equal(await page.locator(selector).evaluate(media => media.paused), true, 'The plugin must not autoplay');
  await page.locator(selector).click(); // An actual user activation, not an autoplay-policy override.
  await page.locator(selector).evaluate(async media => { media.pause(); media.currentTime = 0; media.muted = true; await media.play(); });
  await page.waitForFunction(selector => document.querySelector(selector).currentTime > 0.15, selector);
  return page.locator(selector).evaluate(async media => {
    media.pause(); const playedTo = media.currentTime;
    const seeking = new Promise((resolve, reject) => { const timer = setTimeout(() => reject(new Error('Native media seek did not complete')), 5000); media.addEventListener('seeked', () => { clearTimeout(timer); resolve(); }, { once: true }); });
    media.currentTime = 1.25; await seeking;
    return { duration: media.duration, playedTo, seekedTo: media.currentTime, readyState: media.readyState, error: media.error?.code ?? null, decodedFrames: media.getVideoPlaybackQuality?.().totalVideoFrames ?? null };
  });
}
async function rasterBounds(page) {
  return page.locator('.ol-layer canvas').first().evaluate(canvas => {
    const context = canvas.getContext('2d'), values = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let minX = canvas.width, minY = canvas.height, maxX = -1, maxY = -1, pixels = 0;
    for (let index = 0; index < values.length; index += 4) if (values[index + 3] && values[index] + values[index + 1] + values[index + 2] > 50) {
      const x = index / 4 % canvas.width, y = Math.floor(index / 4 / canvas.width); pixels++; minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
    }
    const rect = canvas.getBoundingClientRect();
    return { pixels, left: rect.x + minX / canvas.width * rect.width, top: rect.y + minY / canvas.height * rect.height, right: rect.x + (maxX + 1) / canvas.width * rect.width, bottom: rect.y + (maxY + 1) / canvas.height * rect.height };
  });
}
async function settledRasterBounds(page) {
  let previous = await rasterBounds(page), stable = 0;
  for (let attempt = 0; attempt < 15; attempt++) {
    await page.waitForTimeout(150);
    const current = await rasterBounds(page);
    if (['left', 'top', 'right', 'bottom'].every(key => Math.abs(current[key] - previous[key]) < 0.5)) stable++; else stable = 0;
    if (stable >= 3) return current;
    previous = current;
  }
  throw new Error('OpenLayers kinetic panning did not settle before band comparison');
}

const treeSource = encode('{"counts":[9007199254740993,2.5],"note":"synthetic <script> is text"}');
const tree = [{ path: '/0', node_type: 'object', attributes: { label: 'root', children_count: 2 } },
  { path: '/0/0', node_type: 'array', attributes: { label: 'counts', children_count: 2 } },
  { path: '/0/0/0', node_type: 'number', attributes: { label: '0', value: '9007199254740993', numeric_representation: 'source lexeme', children_count: 0 } },
  { path: '/0/0/1', node_type: 'number', attributes: { label: '1', value: '2.5', numeric_representation: 'source lexeme', children_count: 0 } },
  { path: '/0/1', node_type: 'string', attributes: { label: 'note', value: 'synthetic <script> is text', children_count: 0 } }];
const zip = new JSZip();
for (let i = 0; i < 205; i++) zip.file(`sample-${String(i).padStart(3, '0')}.txt`, `synthetic-${i}\n`, { date: new Date('2026-01-01T00:00:00Z') });
const zipBytes = await zip.generateAsync({ type: 'uint8array', compression: 'STORE' });
let treeRequests = 0, archiveOffsets = [], rasterRequests = 0, videoBytes;
export const batchOneCases = [
  { name: 'batch-one-structure', component: 'StructuredTreePreview.vue', filename: 'synthetic.json', reader: 'structure', descriptor: { adapter: 'structured-tree' }, bytes: treeSource,
    init: async () => { treeRequests = 0; },
    api: async request => { exactRequest(request, 'batch-one-structure', 'preview'); treeRequests++; return previewReply('batch-one-structure', 'tree', { tree }, { format: 'JSON' }); },
    ready: page => page.getByRole('button', { name: '展开 counts', exact: true }).waitFor(),
    verify: async page => {
      assert.equal(await page.getByText('9007199254740993', { exact: true }).count(), 0);
      await page.getByRole('button', { name: '展开 counts', exact: true }).click(); await page.getByText('9007199254740993', { exact: true }).waitFor();
      await page.getByRole('searchbox').fill('9007199254740993'); assert.equal(await page.getByText('2.5', { exact: true }).count(), 0);
      await page.getByText('counts', { exact: true }).waitFor(); await page.getByRole('searchbox').fill('');
      await page.getByText('synthetic <script> is text', { exact: true }).waitFor();
      assert.equal(treeRequests, 1); return { expanded: true, searchPreservesAncestors: true, exactLargeInteger: true, textEscaped: true, reads: treeRequests };
    } },
  { name: 'batch-one-archive', component: 'ArchivePreview.vue', filename: 'synthetic.zip', reader: 'archive', descriptor: { adapter: 'archive-directory' }, bytes: zipBytes,
    init: async () => { archiveOffsets = []; },
    api: async request => {
      const data = exactRequest(request, 'batch-one-archive', 'preview'), offset = data.options.row_offset; archiveOffsets.push(offset);
      assert.ok([0, 200].includes(offset)); if (archiveOffsets.length > 1) assert.equal(data.version, '1'.repeat(64));
      const rows = Array.from({ length: Math.min(200, 205 - offset) }, (_, index) => { const id = offset + index; return [`sample-${String(id).padStart(3, '0')}.txt`, 'file', `synthetic-${id}\n`.length, `synthetic-${id}\n`.length, false]; });
      return previewReply('batch-one-archive', 'table', { table: { columns: ['成员', '类型', '声明大小（字节）', '压缩大小（字节）', '嵌套压缩包'], rows, row_offset: offset, column_offset: 0, total_rows: 205, total_columns: 5 } }, { format: 'ZIP', listing_only: true, contents_verified: false });
    }, ready: page => page.getByText('sample-000.txt', { exact: true }).waitFor(),
    verify: async page => { await page.getByRole('button', { name: '下一页', exact: true }).click(); await page.getByText('sample-204.txt', { exact: true }).waitFor();
      assert.equal(await page.locator('tbody tr').count(), 5); assert.equal(await page.getByRole('button', { name: '下一页', exact: true }).isDisabled(), true);
      await page.getByRole('searchbox').fill('sample-202'); assert.equal(await page.locator('tbody tr').count(), 1);
      await page.getByRole('button', { name: '上一页', exact: true }).click(); await page.getByText('sample-000.txt', { exact: true }).waitFor();
      assert.deepEqual(archiveOffsets, [0, 200, 0]); return { pages: archiveOffsets, perPage: 200, totalEntries: 205, versionPinned: true, listingOnly: true };
    } },
  { name: 'batch-one-wav', component: 'MediaPreview.vue', filename: 'synthetic.wav', reader: 'binary', descriptor: { adapter: 'audio-waveform' }, bytes: wavBytes(),
    ready: mediaReady('audio'), verify: async page => {
      const media = await realPlayback(page, 'audio'); assert.ok(Math.abs(media.duration - 2) < 0.01); assert.ok(Math.abs(media.seekedTo - 1.25) < 0.01); assert.equal(media.error, null);
      const wave = await page.locator('canvas').evaluate(canvas => { const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data; let nonwhite = 0; for (let i = 0; i < pixels.length; i += 4) if (pixels[i + 3] && pixels[i] < 100) nonwhite++; return { width: canvas.width, height: canvas.height, nonwhite }; });
      assert.equal(wave.width, 1024); assert.equal(wave.height, 300); assert.ok(wave.nonwhite > 1000); return { realNativeAudio: media, waveform: wave };
    }, beforeUnmount: keepMedia('audio'), verifyCleanup: mediaCleanup },
  { name: 'batch-one-video', component: 'MediaPreview.vue', filename: 'synthetic.webm', reader: 'binary', descriptor: { adapter: 'video-player' },
    init: async page => { videoBytes = await createSyntheticVideo(page); },
    api: async request => { exactRequest(request, 'batch-one-video', 'bytes'); assert.ok(videoBytes?.length > 100); return bytesReply('batch-one-video', videoBytes); },
    ready: mediaReady('video'), verify: async page => {
      const media = await realPlayback(page, 'video'); assert.ok(Math.abs(media.duration - 2) < 0.05); assert.ok(Math.abs(media.seekedTo - 1.25) < 0.05); assert.ok(media.decodedFrames > 0); assert.equal(media.error, null);
      const frame = await page.locator('video').evaluate(video => { const canvas = document.createElement('canvas'); canvas.width = video.videoWidth; canvas.height = video.videoHeight; const context = canvas.getContext('2d'); context.drawImage(video, 0, 0); const pixel = Array.from(context.getImageData(5, 5, 1, 1).data); const result = { width: video.videoWidth, height: video.videoHeight, pixel }; canvas.width = 0; canvas.height = 0; return result; });
      assert.equal(frame.width, 128); assert.equal(frame.height, 96); assert.ok(frame.pixel[2] > frame.pixel[0]); return { generatedWithRealMediaRecorder: true, realNativeVideo: media, decodedBlueFrameAfterSeek: frame, fixtureBytes: videoBytes.length };
    }, beforeUnmount: keepMedia('video'), verifyCleanup: mediaCleanup },
  { name: 'batch-one-geotiff', component: 'domains/OpenLayersPreview.vue', filename: 'synthetic-multiband.tif', reader: 'binary', descriptor: { adapter: 'openlayers' }, bytes: multibandTiff(),
    init: async () => { rasterRequests = 0; },
    api: async request => { exactRequest(request, 'batch-one-geotiff', 'bytes'); rasterRequests++; return bytesReply('batch-one-geotiff', multibandTiff()); },
    ready: async page => { await page.getByText('波段 1 / 2', { exact: false }).waitFor(); await page.locator('.ol-layer canvas').first().waitFor(); await page.waitForTimeout(250); },
    verify: async page => {
      await page.getByText('NoData = 65535', { exact: false }).waitFor(); await page.getByText('本次预览 1 个无效像素', { exact: false }).waitFor();
      assert.match(await page.locator('.legend').textContent(), /99\.000/);
      const initial = await rasterBounds(page), viewport = await page.locator('.ol-viewport').boundingBox(); assert.ok(initial.pixels > 1000);
      await page.mouse.move(viewport.x + viewport.width / 2, viewport.y + viewport.height / 2); await page.mouse.down();
      await page.mouse.move(viewport.x + viewport.width / 2 + 70, viewport.y + viewport.height / 2 + 20, { steps: 6 }); await page.mouse.up(); await page.waitForTimeout(300);
      const moved = await settledRasterBounds(page); assert.ok(Math.abs(moved.left - initial.left) > 30, 'Actual OL panning must move the rendered extent');
      await page.getByRole('combobox').selectOption('2'); await page.getByText('波段 2 / 2', { exact: false }).waitFor(); await page.waitForTimeout(250);
      await page.getByText('本次预览 0 个无效像素', { exact: false }).waitFor(); assert.match(await page.locator('.legend').textContent(), /1199\.0/);
      const after = await settledRasterBounds(page); assert.ok(after.pixels > 1000);
      assert.ok(Math.abs(after.left - moved.left) < 4 && Math.abs(after.top - moved.top) < 4, `Band switch must preserve the panned map view: ${JSON.stringify({ moved, after })}`);
      assert.equal(rasterRequests, 1); assert.equal(await page.evaluate(() => window.__activeBlobs.size), 1);
      return { actualGeoTiffTwoBands: true, authorizedReads: rasterRequests, noDataCounts: [1, 0], visibleRanges: [[0, 99], [1000, 1199]], preservedView: { before: moved, after }, liveRasterBlobs: 1 };
    } },
];
