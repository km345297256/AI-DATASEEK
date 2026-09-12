import type { VisualizationPlugin } from './contract';

export interface ContentProfile {
  profile_version: 1;
  container: 'unknown' | 'netcdf' | 'hdf5' | 'mat' | 'tiff';
  dialect: 'unknown' | 'netcdf-classic' | 'netcdf-64bit-offset' | 'netcdf-cdf5' | 'hdf5' | 'matlab-level5' | 'matlab-v7.3' | 'tiff' | 'bigtiff';
  traits: ('numeric-container' | 'geotiff' | 'ome')[];
  evidence: string[];
  bytes_read: number;
  truncated: boolean;
}
const fields = ['profile_version', 'container', 'dialect', 'traits', 'evidence', 'bytes_read', 'truncated'];
const containers = ['unknown', 'netcdf', 'hdf5', 'mat', 'tiff'];
const dialects = ['unknown', 'netcdf-classic', 'netcdf-64bit-offset', 'netcdf-cdf5', 'hdf5', 'matlab-level5', 'matlab-v7.3', 'tiff', 'bigtiff'];
export function parseContentProfile(value: unknown): ContentProfile {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('内容画像无效。');
  const p = value as Record<string, unknown>;
  if (Object.keys(p).length !== fields.length || Object.keys(p).some(key => !fields.includes(key))
    || p.profile_version !== 1 || typeof p.container !== 'string' || !containers.includes(p.container) || typeof p.dialect !== 'string' || !dialects.includes(p.dialect)
    || !Array.isArray(p.traits) || p.traits.length > 3 || new Set(p.traits).size !== p.traits.length || p.traits.some(v => !['numeric-container', 'geotiff', 'ome'].includes(v))
    || !Array.isArray(p.evidence) || p.evidence.length > 32 || p.evidence.some(v => typeof v !== 'string' || v.length > 128)
    || !Number.isSafeInteger(p.bytes_read) || Number(p.bytes_read) < 0 || Number(p.bytes_read) > 65536 || typeof p.truncated !== 'boolean') throw new Error('内容画像不符合批准协议。');
  return p as unknown as ContentProfile;
}
export function canProfileFilename(filename: string): boolean {
  return /\.(?:tiff?|mat|h5|hdf5|nc4?|netcdf|nxs|nx)$/i.test(filename);
}
/** Content only recommends among enabled filename matches; it never grants a new capability. */
export function profileCandidates(candidates: readonly VisualizationPlugin[], profile: ContentProfile | null): VisualizationPlugin[] {
  if (!profile) return [...candidates];
  const score = (plugin: VisualizationPlugin) => {
    if (profile.container === 'netcdf') return plugin.reader === 'netcdf' ? 30 : plugin.adapter === 'h5web' ? -30 : 0;
    if (profile.container === 'hdf5' || profile.dialect === 'matlab-v7.3') {
      // Keep the established NetCDF map/series default: HDF5 magic alone does
      // not prove NetCDF4 or CF coordinates, and must not hijack that workflow.
      if (profile.dialect !== 'matlab-v7.3' && candidates.some(p => p.reader === 'netcdf')) return 0;
      return plugin.adapter === 'h5web' ? 30 : 0;
    }
    if (profile.dialect === 'matlab-level5') return plugin.adapter === 'plotly' ? 30 : plugin.adapter === 'h5web' ? -30 : 0;
    if (profile.container === 'tiff') {
      if (profile.traits.includes('ome') && plugin.adapter === 'viv') return 40;
      if (profile.traits.includes('geotiff') && plugin.adapter === 'openlayers') return 30;
      if (plugin.adapter === 'tiff') return 10;
    }
    return 0;
  };
  return [...candidates].sort((a, b) => score(b) - score(a));
}
export function profileLabel(profile: ContentProfile): string {
  const names = { unknown: '格式尚未确证', 'netcdf-classic': '经典 NetCDF', 'netcdf-64bit-offset': 'NetCDF 64-bit offset', 'netcdf-cdf5': 'NetCDF CDF5', hdf5: 'HDF5 容器（不据此推断 NetCDF4）', 'matlab-level5': 'MATLAB Level 5', 'matlab-v7.3': 'MATLAB v7.3 / HDF5', tiff: 'TIFF', bigtiff: 'BigTIFF' };
  return `${names[profile.dialect]}${profile.traits.includes('ome') ? ' · OME 显微元数据' : ''}${profile.traits.includes('geotiff') ? ' · 地理标签' : ''}${profile.truncated ? ' · 元数据检查未完整' : ''}`;
}
