/** Render numerical samples without inventing continuity across missing values. */
export function finiteExtent(values: readonly (number | null)[]): [number, number] | null {
  const finite = values.filter((item): item is number => typeof item === 'number' && Number.isFinite(item));
  if (!finite.length) return null;
  let low = Math.min(...finite), high = Math.max(...finite);
  if (low === high) { const margin = Math.abs(low) * .05 || 1; low -= margin; high += margin; }
  return [low, high];
}
export function seriesSegments(x: readonly number[], y: readonly (number | null)[]): Array<Array<[number, number]>> {
  const result: Array<Array<[number, number]>> = [];
  let current: Array<[number, number]> = [];
  for (let index = 0; index < Math.min(x.length, y.length); index++) {
    const horizontal = x[index], vertical = y[index];
    if (!Number.isFinite(horizontal) || vertical === null || !Number.isFinite(vertical)) {
      if (current.length) result.push(current);
      current = [];
    } else current.push([horizontal, vertical]);
  }
  if (current.length) result.push(current);
  return result;
}
/** Midpoints preserve nonuniform/strided coordinates instead of stretching the last cell. */
export function coordinateEdges(centers: readonly number[], low = -Infinity, high = Infinity): number[] {
  if (!centers.length || centers.some((value) => !Number.isFinite(value))) return [];
  const clamp = (value: number) => Math.max(low, Math.min(high, value));
  if (centers.length === 1) return [clamp(centers[0]! - .5), clamp(centers[0]! + .5)];
  const edges = [centers[0]! - (centers[1]! - centers[0]!) / 2];
  for (let index = 1; index < centers.length; index++) edges.push((centers[index - 1]! + centers[index]!) / 2);
  edges.push(centers[centers.length - 1]! + (centers[centers.length - 1]! - centers[centers.length - 2]!) / 2);
  return edges.map(clamp);
}
export function heatColor(value: number | null, extent: [number, number] | null): string {
  if (value === null || !Number.isFinite(value) || !extent) return '#edf0f2';
  const ratio = Math.min(1, Math.max(0, (value - extent[0]) / (extent[1] - extent[0])));
  return `hsl(${240 - ratio * 235}, 68%, ${40 + ratio * 10}%)`;
}
export function tickLabel(value: number): string {
  return Number.isFinite(value) ? Number(value.toPrecision(5)).toString() : '—';
}
