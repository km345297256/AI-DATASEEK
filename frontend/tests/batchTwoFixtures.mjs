export const version = '1'.repeat(64);
export const memberId = 'member-' + 'a'.repeat(64);
const result = (kind, payload, metadata) => ({ kind, payload, metadata, version, revision: '2'.repeat(64), warnings: [], sampled: false });
export function signalFixture({ channels = [0], start_seconds = 0, duration_seconds = 1 } = {}) {
  const choices = [{ id: 0, label: 'EEG Fp1', unit: 'uV', sample_rate: 4, selectable: true, channel_type: 'signal' },
    { id: 1, label: 'ECG', unit: 'mV', sample_rate: 2, selectable: true, channel_type: 'signal' },
    { id: 2, label: 'EDF Annotations', unit: '', sample_rate: 1, selectable: false, channel_type: 'annotation' }];
  return result('series', { choices: { channels: choices }, selected: { channels, start_seconds, duration_seconds },
    series: channels.map(id => { const choice = choices[id], count = Math.floor(duration_seconds * choice.sample_rate); return { channel: id, label: choice.label, unit: choice.unit, sample_rate: choice.sample_rate,
      x: Array.from({ length: count }, (_, i) => start_seconds + i / choice.sample_rate), y: Array.from({ length: count }, (_, i) => (id ? 0.2 : 2) * Math.cos(i)) }; }) },
  { format: 'edf', total_duration_seconds: 1000000, source_bytes: 2500000768, read_bytes: 1268, read_requests: 4, identity_fields_hidden: true, annotations_hidden: true, no_resampling: true });
}
export function mcaFixture(calibrated = false) {
  const counts = [0, 2, 10, 9007199254740991];
  return result('array', { array: { shape: [4, 2], dimensions: [calibrated ? 'energy (keV)' : 'channel', 'counts'], values: counts.flatMap((count, i) => [calibrated ? 1 + i * 0.5 : i, count]) } },
    { format: 'mca', dialect: 'amptek-pmca-ascii', channels: 4, axis: calibrated ? 'energy' : 'channel', energy_unit: calibrated ? 'keV' : null,
      calibration_applied: calibrated, calibration_coefficients: calibrated ? [1, 0.5, 0] : null, raw_counts: true, fitting: false, header_text_hidden: true, live_time_seconds: 10, real_time_seconds: 12 });
}
export function archiveFixture({ member_id = null, row_offset = 0 } = {}) {
  const meta = { format: 'zip', source_bytes: 10000, member_limit_bytes: 262144, writes_source: false, recursive: false };
  const table = { column_offset: 0, row_offset, total_rows: member_id ? 201 : 2, total_columns: member_id ? 2 : 5 };
  if (member_id) return result('table', { table: { ...table, columns: ['行号', '文本'], rows: Array.from({ length: Math.min(200, 201 - row_offset) }, (_, i) => [row_offset + i + 1, i === 0 ? '<script>window.__archiveExecuted=true</script>' : `sample ${row_offset + i + 1}`]) } },
    { ...meta, mode: 'text', member_id, member_name: 'samples.txt', member_bytes: 9000, checksum_verified: true, encoding: 'utf-8', line_char_limit: 1024, member_text_only: true });
  return result('table', { table: { ...table, columns: ['成员', '类型', '声明大小（字节）', '可预览', '预览标识'], rows: [['samples.txt', 'file', 9000, true, memberId], ['folder/', 'directory', 0, false, null]] } }, { ...meta, mode: 'directory', contents_verified: false });
}
