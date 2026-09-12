/** Feature-level, closed-boundary selection in the geometry's own coordinates. */
export type Point = [number, number];
export type Bounds = [number, number, number, number];
export type Geometry =
  | { type: 'Point'; coordinates: Point }
  | { type: 'MultiPoint'; coordinates: Point[] }
  | { type: 'LineString'; coordinates: Point[] }
  | { type: 'MultiLineString'; coordinates: Point[][] }
  | { type: 'Polygon'; coordinates: Point[][] }
  | { type: 'MultiPolygon'; coordinates: Point[][][] };

const coordinateDepth: Record<string, number> = {
  Point: 0, MultiPoint: 1, LineString: 1, MultiLineString: 2, Polygon: 2, MultiPolygon: 3,
};

function visitPoints(value: unknown, depth: number, visit: (point: Point) => void): boolean {
  if (!Array.isArray(value)) return false;
  if (depth === 0) {
    if (value.length !== 2 || !Number.isFinite(value[0]) || !Number.isFinite(value[1])) return false;
    visit(value as Point);
    return true;
  }
  for (const child of value) if (!visitPoints(child, depth - 1, visit)) return false;
  return true;
}

/** Empty or malformed/non-finite geometries never produce partial bounds. */
export function geometryBounds(geometry: Geometry | null): Bounds | null {
  if (!geometry || typeof geometry !== 'object') return null;
  const depth = coordinateDepth[geometry.type];
  if (typeof depth !== 'number') return null;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const valid = visitPoints(geometry.coordinates, depth, ([x, y]) => {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  });
  return valid && minX !== Infinity ? [minX, minY, maxX, maxY] : null;
}

function validBounds(bounds: Bounds): boolean {
  return Array.isArray(bounds) && bounds.length === 4 && bounds.every(Number.isFinite)
    && bounds[0] <= bounds[2] && bounds[1] <= bounds[3];
}

function containsPoint([x, y]: Point, bounds: Bounds): boolean {
  return x >= bounds[0] && x <= bounds[2] && y >= bounds[1] && y <= bounds[3];
}

function fraction(value: number, start: number, end: number): number {
  const delta = end - start;
  if (Number.isFinite(delta)) return (value - start) / delta;
  // Finite coordinates may still overflow when subtracted. Scaling this axis
  // preserves its interpolation parameter without imposing coordinate limits.
  const scale = Math.max(Math.abs(value), Math.abs(start), Math.abs(end));
  return (value / scale - start / scale) / (end / scale - start / scale);
}

function segmentIntersectsBounds(start: Point, end: Point, bounds: Bounds): boolean {
  let enter = 0, leave = 1;
  // Slab clipping includes tangencies, collinear edges and zero-area boxes.
  for (let axis = 0; axis < 2; axis++) {
    const a = start[axis]!, b = end[axis]!;
    const low = bounds[axis]!, high = bounds[axis + 2]!;
    if (a === b) {
      if (a < low || a > high) return false;
      continue;
    }
    let first = fraction(low, a, b), last = fraction(high, a, b);
    if (first > last) [first, last] = [last, first];
    if (first > enter) enter = first;
    if (last < leave) leave = last;
    if (enter > leave) return false;
  }
  return true;
}

function lineIntersectsBounds(points: Point[], bounds: Bounds, close = false): boolean {
  if (!points.length) return false;
  if (containsPoint(points[0]!, bounds)) return true;
  for (let index = 1; index < points.length; index++) {
    if (segmentIntersectsBounds(points[index - 1]!, points[index]!, bounds)) return true;
  }
  return close && segmentIntersectsBounds(points[points.length - 1]!, points[0]!, bounds);
}

/** Boundary intersections are checked separately before this parity test. */
function insideRing(point: Point, ring: Point[]): boolean {
  let inside = false;
  for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index++) {
    const start = ring[previous]!, end = ring[index]!;
    if ((start[1] > point[1]) === (end[1] > point[1])) continue;
    // Avoid product overflow in the usual ray/segment cross-product formula.
    if (start[0] > point[0] && end[0] > point[0]) {
      inside = !inside;
    } else if (start[0] > point[0] || end[0] > point[0]) {
      const alongY = fraction(point[1], start[1], end[1]);
      const alongX = fraction(point[0], start[0], end[0]);
      if (end[0] > start[0] ? alongY > alongX : alongY < alongX) inside = !inside;
    }
  }
  return inside;
}

function boundsContain(outer: Bounds, inner: Bounds): boolean {
  return outer[0] <= inner[0] && outer[1] <= inner[1]
    && outer[2] >= inner[2] && outer[3] >= inner[3];
}

function onRingBoundary(point: Point, ring: Point[]): boolean {
  const box: Bounds = [point[0], point[1], point[0], point[1]];
  for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index++) {
    if (segmentIntersectsBounds(ring[previous]!, ring[index]!, box)) return true;
  }
  return false;
}

function ringContainsRing(outer: Point[], inner: Point[]): boolean {
  // Valid Shapefile rings do not cross. A non-boundary point therefore decides
  // containment; do not mistake a shared/tangent vertex for an interior point.
  for (const point of inner) {
    if (!onRingBoundary(point, outer)) return insideRing(point, outer);
  }
  // A diamond inscribed in a square can have every vertex on the boundary.
  // Its edge midpoints still establish strict containment. Identical rings
  // never become mutual parents merely because their boundaries coincide.
  for (let index = 0, previous = inner.length - 1; index < inner.length; previous = index++) {
    const start = inner[previous]!, end = inner[index]!;
    const midpoint: Point = [start[0] / 2 + end[0] / 2, start[1] / 2 + end[1] / 2];
    if (!onRingBoundary(midpoint, outer)) return insideRing(midpoint, outer);
  }
  return false;
}

/**
 * Convert non-crossing SHP rings to explicit Polygon/MultiPolygon topology.
 * Even nesting depths are exterior boundaries; immediate odd-depth children
 * are holes. Neither ring winding nor file order controls the interpretation.
 * This groups valid rings; it does not repair crossing/self-intersecting data.
 */
export function polygonFromShapefileRings(rings: Point[][]): Geometry {
  if (!Array.isArray(rings)) throw new Error('Invalid Shapefile polygon rings');
  const entries: { ring: Point[]; bounds: Bounds }[] = [];
  for (const ring of rings) {
    if (!Array.isArray(ring)) throw new Error('Invalid Shapefile polygon rings');
    if (!ring.length) continue;
    const bounds = geometryBounds({ type: 'LineString', coordinates: ring });
    if (!bounds) throw new Error('Invalid Shapefile polygon coordinates');
    entries.push({ ring, bounds });
  }
  if (!entries.length) return { type: 'Polygon', coordinates: [] };
  const parents = entries.map(() => -1);
  for (let child = 0; child < entries.length; child++) {
    for (let candidate = 0; candidate < entries.length; candidate++) {
      if (candidate === child || !boundsContain(entries[candidate]!.bounds, entries[child]!.bounds)) continue;
      const current = parents[child]!;
      // Once a parent is known, only a ring inside its bounds could be nearer.
      if (current !== -1 && !boundsContain(entries[current]!.bounds, entries[candidate]!.bounds)) continue;
      if (ringContainsRing(entries[candidate]!.ring, entries[child]!.ring)) parents[child] = candidate;
    }
  }
  const depths = entries.map(() => -1);
  for (let index = 0; index < entries.length; index++) {
    if (depths[index] !== -1) continue;
    const chain: number[] = [], visited = new Set<number>();
    let cursor = index;
    while (cursor !== -1 && depths[cursor] === -1) {
      if (visited.has(cursor)) throw new Error('Invalid Shapefile polygon nesting');
      visited.add(cursor);
      chain.push(cursor);
      cursor = parents[cursor]!;
    }
    let depth = cursor === -1 ? -1 : depths[cursor]!;
    for (let position = chain.length - 1; position >= 0; position--) depths[chain[position]!] = ++depth;
  }
  const polygons: Point[][][] = [], exteriors = new Map<number, Point[][]>();
  for (let index = 0; index < entries.length; index++) {
    if (depths[index]! % 2 === 0) {
      const polygon = [entries[index]!.ring];
      polygons.push(polygon);
      exteriors.set(index, polygon);
    }
  }
  for (let index = 0; index < entries.length; index++) {
    if (depths[index]! % 2 === 1) exteriors.get(parents[index]!)!.push(entries[index]!.ring);
  }
  return polygons.length === 1
    ? { type: 'Polygon', coordinates: polygons[0]! }
    : { type: 'MultiPolygon', coordinates: polygons };
}

function polygonIntersectsBounds(rings: Point[][], bounds: Bounds): boolean {
  const outer = rings[0];
  if (!outer?.length) return false;
  // A hole's boundary belongs to the polygon; its open interior does not.
  for (const ring of rings) if (lineIntersectsBounds(ring, bounds, true)) return true;
  const corners: Point[] = [[bounds[0], bounds[1]], [bounds[0], bounds[3]],
    [bounds[2], bounds[1]], [bounds[2], bounds[3]]];
  return corners.some(point => insideRing(point, outer)
    && !rings.some((ring, index) => index > 0 && insideRing(point, ring)));
}

/** True for actual geometry contact, not merely overlapping feature bboxes. */
export function geometryIntersectsBounds(geometry: Geometry | null, bounds: Bounds): boolean {
  if (!validBounds(bounds)) return false;
  const extent = geometryBounds(geometry);
  if (!extent || !geometry || extent[2] < bounds[0] || extent[0] > bounds[2]
      || extent[3] < bounds[1] || extent[1] > bounds[3]) return false;
  switch (geometry.type) {
    case 'Point': return containsPoint(geometry.coordinates, bounds);
    case 'MultiPoint': return geometry.coordinates.some(point => containsPoint(point, bounds));
    case 'LineString': return lineIntersectsBounds(geometry.coordinates, bounds);
    case 'MultiLineString': return geometry.coordinates.some(line => lineIntersectsBounds(line, bounds));
    case 'Polygon': return polygonIntersectsBounds(geometry.coordinates, bounds);
    case 'MultiPolygon': return geometry.coordinates.some(polygon => polygonIntersectsBounds(polygon, bounds));
  }
}
