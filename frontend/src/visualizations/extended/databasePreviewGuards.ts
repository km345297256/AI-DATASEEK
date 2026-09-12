/** Shared bounded JSON primitives, never a query or executable configuration. */
export function need(ok: unknown): asserts ok { if (!ok) throw new Error('数据库文件预览载荷无效，请重新打开预览。'); }
export const object = (v: unknown): v is Record<string, any> => v !== null && typeof v === 'object' && !Array.isArray(v);
export function keys(v: unknown, fields: string): asserts v is Record<string, any> {
  need(object(v) && Object.keys(v).sort().join('|') === fields.split(' ').filter(Boolean).sort().join('|'));
}
export const int = (v: unknown, low = 0, high = Number.MAX_SAFE_INTEGER): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= low && v <= high;
export const bytes = (v: string) => new TextEncoder().encode(v).length;
export const safeText = (v: unknown): v is string => typeof v === 'string' && bytes(v) <= 512 && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(v) && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|(?:\/Users\/|\/home\/|\/tmp\/|\/private\/|\/var\/|file:|https?:\/\/|[A-Za-z]:\\)/i.test(v);
export const safeName = (v: unknown): v is string => safeText(v) && bytes(v) <= 128 && v === v.trim() && /^[\p{L}\p{N}_][\p{L}\p{N}_ .()-]{0,127}$/u.test(v);
export const same = (a: any, b: any): boolean => Array.isArray(a) && Array.isArray(b) ? a.length === b.length && a.every((v, i) => same(v, b[i])) : object(a) && object(b) ? Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k => Object.prototype.hasOwnProperty.call(b, k) && same(a[k], b[k])) : a === b;
export const intText = (v: unknown, low = -(2n ** 63n), high = 2n ** 63n - 1n): v is string => typeof v === 'string' && v.length <= 21 && v === v.trim() && /^(?:0|-?[1-9][0-9]*)$/.test(v) && BigInt(v) >= low && BigInt(v) <= high;
