import get from 'lodash/get';
import set from 'lodash/set';
import { useCallback, useMemo, useState } from 'react';

/**
 * NMRium's embedding preferences are authoritative. The upstream application
 * also restores saved workspaces before applying those preferences; this
 * build-only bridge removes that persistent input and all persistent writes.
 * Optional panel state, if ever displayed, lives only in its mounted React hook.
 */
export function getLocalStorage(_storageKey: string, _isJson = true): null { return null; }
export function storeData(_storageKey: string, _value: unknown): void {}
export function removeData(_storageKey: string): void {}
export function getValue(object: unknown, keyPath: string, defaultValue: unknown = null): unknown {
  return get(object, keyPath, defaultValue);
}
export function useStateWithLocalStorage(_storageKey: string, key?: string) {
  const [value, setValue] = useState<Record<string, unknown>>({});
  const setData = useCallback((data: Record<string, unknown>, path?: string | null) => {
    setValue((previous) => path ? set(structuredClone(previous), path, data) : { ...previous, ...data });
  }, []);
  return useMemo(() => [key ? get(value, key, {}) : value, setData] as const, [key, value, setData]);
}
