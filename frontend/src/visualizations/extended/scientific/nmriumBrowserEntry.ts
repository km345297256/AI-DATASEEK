// Standalone trusted browser entry. Keep React and its renderer together so
// loading this scientific workbench does not expand the application's Rollup
// graph or create cross-runtime hook calls.
import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { NMRium } from 'nmrium';
import '@blueprintjs/core/lib/css/blueprint.css';
import '@blueprintjs/icons/lib/css/blueprint-icons.css';
import { nmriumReadOnlyPreferences } from './nmriumReadOnly';
export const version = '0.60.0';
export interface NumericNmrSpectrum { x: number[]; y: number[]; nucleus: string; frequency?: number; }
export function mountNmr(target: HTMLElement, input: NumericNmrSpectrum, onError?: () => void): () => void {
  if (!Array.isArray(input.x) || !Array.isArray(input.y) || input.x.length < 2 || input.x.length > 8192 || input.y.length !== input.x.length || !input.x.every(Number.isFinite) || !input.y.every(Number.isFinite) || !/^\d{1,3}[A-Z][a-z]?$/.test(input.nucleus)) throw new Error('核磁谱图输入不符合数值预览协议。');
  const spectrum = { id: 'dataseek-readonly', data: { x: [...input.x], re: [...input.y] }, info: { nucleus: input.nucleus, dimension: 1 as const, isFid: false, isComplex: false, name: 'Local processed spectrum', ...(Number.isFinite(input.frequency) && input.frequency! > 0 ? { originFrequency: input.frequency } : {}) }, filters: [] };
  const root = createRoot(target);
  root.render(createElement(NMRium, { data: { spectra: [spectrum] }, workspace: 'dataseek-readonly', customWorkspaces: { 'dataseek-readonly': { label: 'DataSeek read-only', ...nmriumReadOnlyPreferences } }, preferences: nmriumReadOnlyPreferences, onError }));
  return () => root.unmount();
}
