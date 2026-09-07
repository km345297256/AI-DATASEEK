import type { DataCenterDataset } from '../api/dataset.ts';

export type DatasetSourceGroup = 'local' | 'international' | 'scidb' | 'tpdc' | 'chemdc' | 'ngdc';

export function datasetSourceGroup(dataset: DataCenterDataset): DatasetSourceGroup {
  const source = catalogText(dataset, 'source_catalog');
  if (['scidb', 'tpdc', 'chemdc', 'ngdc'].includes(source)) return source as DatasetSourceGroup;
  return dataset.metadata?.curated === true ? 'international' : 'local';
}

export function catalogText(dataset: DataCenterDataset, key: string): string {
  const value = dataset.metadata?.[key];
  return typeof value === 'string' ? value : '';
}

export function publicSourceUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    // ChemDC currently serves its public catalog over HTTP only. Keep this
    // exception exact; arbitrary HTTP/private/file provenance is not allowed.
    const publicProtocol = url.protocol === 'https:' || (
      url.protocol === 'http:' && url.hostname === 'chemdc.casdc.cn' && !url.port
    );
    return publicProtocol && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

export function datasetMatches(dataset: DataCenterDataset, domain: string, query: string): boolean {
  if (domain && (dataset.domain || 'general') !== domain) return false;
  const text = [dataset.name, dataset.description, dataset.data_center_name, ...dataset.tags,
    catalogText(dataset, 'publisher')].join(' ').toLocaleLowerCase();
  return text.includes(query.trim().toLocaleLowerCase());
}

export function datasetBytes(dataset: DataCenterDataset): number {
  return dataset.files.reduce((sum, file) => sum + Math.max(0, Number(file.size) || 0), 0);
}

export function formatDatasetBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}
