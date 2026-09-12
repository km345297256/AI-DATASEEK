import assert from 'node:assert/strict';
import { test } from 'node:test';
import { geometryBounds, geometryIntersectsBounds, polygonFromShapefileRings } from '../src/components/filePreviews/shapefileSelection.ts';

const shape = (type, coordinates) => ({ type, coordinates });
const ring = (low = 0, high = 10) => [[low, low], [high, low], [high, high], [low, high], [low, low]];
const hit = (type, coordinates, bounds) => geometryIntersectsBounds(shape(type, coordinates), bounds);

for (const [type, coordinates, expected] of [
  ['Point', [4, -2], [4, -2, 4, -2]],
  ['MultiPoint', [[-8, 3], [2, -5], [1, 6]], [-8, -5, 2, 6]],
  ['LineString', [[2, 8], [-1, -4]], [-1, -4, 2, 8]],
  ['MultiLineString', [[], [[-1, 2]], [[3, -4], [6, 9]]], [-1, -4, 6, 9]],
  ['Polygon', [ring(), ring(3, 7)], [0, 0, 10, 10]],
  ['MultiPolygon', [[ring()], [ring(-20, -10)]], [-20, -20, 10, 10]],
]) {
  test(`${type} computes bounds without flattening vertices into features`, () => {
    assert.deepEqual(geometryBounds(shape(type, coordinates)), expected);
  });
}

test('points and multipoints use closed boundaries without a tolerance expansion', () => {
  assert.equal(hit('Point', [0, 1], [0, 0, 1, 1]), true);
  assert.equal(hit('Point', [1 + Number.EPSILON, 1], [0, 0, 1, 1]), false);
  assert.equal(hit('MultiPoint', [[-1, 2], [2, -1]], [0, 0, 1, 1]), false);
  assert.equal(hit('MultiPoint', [[-1, 2], [1, 1], [2, -1]], [0, 0, 1, 1]), true);
});

test('a segment crossing the box is selected even when both endpoints are outside', () => {
  assert.equal(hit('LineString', [[-5, 0], [5, 0]], [-1, -1, 1, 1]), true);
  assert.equal(hit('LineString', [[-5, -5], [5, 5]], [-1, -1, 1, 1]), true);
  assert.equal(hit('LineString', [[5, 5], [-5, -5]], [-1, -1, 1, 1]), true);
  assert.equal(hit('LineString', [[-2, 1], [1, -2]], [0, 0, 1, 1]), false);
});

test('bbox overlap alone does not select an L-shaped line or separate multi-lines', () => {
  const lines = [[[-5, -5], [-5, 5]], [[5, -5], [5, 5]]];
  assert.equal(hit('LineString', [[0, 10], [0, 0], [10, 0]], [4, 4, 6, 6]), false);
  assert.equal(hit('MultiLineString', lines, [-1, -1, 1, 1]), false);
  assert.equal(hit('MultiLineString', [...lines, [[-5, 0], [5, 0]]], [-1, -1, 1, 1]), true);
});

test('tangent, collinear, duplicate and single-point line boundaries are closed', () => {
  for (const points of [[[-1, 1], [1, -1]], [[-1, 0], [2, 0]], [[0, 0], [0, 0]], [[1, 1]]]) {
    assert.equal(hit('LineString', points, [0, 0, 1, 1]), true);
  }
  assert.equal(hit('LineString', [[-1, 2], [2, 2]], [0, 0, 1, 1]), false);
});

test('slab clipping agrees with independent exact integer edge tests in every direction', () => {
  const orient = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const on = (a, b, c) => orient(a, b, c) === 0 && c[0] >= Math.min(a[0], b[0])
    && c[0] <= Math.max(a[0], b[0]) && c[1] >= Math.min(a[1], b[1]) && c[1] <= Math.max(a[1], b[1]);
  const edgesMeet = (a, b, c, d) => on(a, b, c) || on(a, b, d) || on(c, d, a) || on(c, d, b)
    || (orient(a, b, c) * orient(a, b, d) < 0 && orient(c, d, a) * orient(c, d, b) < 0);
  const inside = (point, box) => box[0] <= point[0] && point[0] <= box[2] && box[1] <= point[1] && point[1] <= box[3];
  const points = [];
  for (let x = -2; x <= 2; x++) for (let y = -2; y <= 2; y++) points.push([x, y]);
  for (const box of [[0, 0, 1, 1], [0, 0, 0, 0], [-1, 0, 1, 0], [0, -1, 0, 1]]) {
    const corners = [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]];
    for (const a of points) for (const b of points) {
      const expected = inside(a, box) || inside(b, box)
        || corners.some((corner, index) => edgesMeet(a, b, corner, corners[(index + 1) % 4]));
      assert.equal(hit('LineString', [a, b], box), expected, JSON.stringify({ a, b, box }));
    }
  }
});

test('polygons select enclosed boxes, enclosed polygons and crossing boundaries', () => {
  assert.equal(hit('Polygon', [ring()], [4, 4, 6, 6]), true);
  assert.equal(hit('Polygon', [ring()], [-1, -1, 11, 11]), true);
  assert.equal(hit('Polygon', [ring()], [-1, 4, 1, 6]), true);
  assert.equal(hit('Polygon', [ring()], [10, 2, 11, 3]), true);
  assert.equal(hit('Polygon', [ring()], [11, 2, 12, 3]), false);
});

test('polygon hole interiors are excluded but hole boundaries remain selectable', () => {
  const polygon = [ring(), ring(3, 7)];
  assert.equal(hit('Polygon', polygon, [4, 4, 6, 6]), false);
  assert.equal(hit('Polygon', polygon, [3, 4, 3, 6]), true);
  assert.equal(hit('Polygon', polygon, [2, 4, 4, 6]), true);
  assert.equal(hit('Polygon', polygon, [1, 1, 2, 2]), true);
  assert.equal(hit('Polygon', polygon.map(points => [...points].reverse()), [4, 4, 6, 6]), false);
});

test('unclosed rings are closed implicitly and concave empty areas are not selected', () => {
  const concave = [[0, 0], [6, 0], [6, 2], [2, 2], [2, 6], [0, 6]];
  assert.equal(hit('Polygon', [concave], [3, 3, 5, 5]), false);
  assert.equal(hit('Polygon', [concave], [-1, 3, 1, 5]), true);
  assert.equal(hit('Polygon', [ring().slice(0, -1)], [4, 4, 6, 6]), true);
});

test('multiple holes and multipolygons preserve individual polygon interiors', () => {
  const first = [ring(), ring(1, 3), ring(6, 9)];
  assert.equal(hit('Polygon', first, [7, 7, 8, 8]), false);
  assert.equal(hit('Polygon', first, [4, 4, 5, 5]), true);
  assert.equal(hit('MultiPolygon', [first, [ring(20, 30)]], [12, 12, 18, 18]), false);
  assert.equal(hit('MultiPolygon', [first, [ring(20, 30)]], [24, 24, 26, 26]), true);
  // A second polygon can be an island within the first polygon's hole.
  assert.equal(hit('MultiPolygon', [[ring(), ring(3, 7)], [ring(4, 6)]], [4.5, 4.5, 5.5, 5.5]), true);
});

test('zero-width, zero-height and point selection boxes work for every dimension', () => {
  assert.equal(hit('Point', [3, 3], [3, 3, 3, 3]), true);
  assert.equal(hit('LineString', [[0, 0], [6, 6]], [3, 3, 3, 3]), true);
  assert.equal(hit('LineString', [[0, 0], [6, 6]], [3, 4, 3, 4]), false);
  assert.equal(hit('LineString', [[0, 3], [6, 3]], [3, 1, 3, 5]), true);
  assert.equal(hit('LineString', [[3, 0], [3, 6]], [1, 3, 5, 3]), true);
  assert.equal(hit('Polygon', [ring(), ring(3, 7)], [5, 5, 5, 5]), false);
  assert.equal(hit('Polygon', [ring(), ring(3, 7)], [3, 5, 3, 5]), true);
  assert.equal(hit('Polygon', [ring()], [5, 5, 5, 5]), true);
});

test('invalid, empty and nonfinite coordinates fail closed rather than making partial bounds', () => {
  for (const geometry of [null, {}, shape('Unknown', []), shape('Point', []), shape('Point', ['1', 2]),
    shape('Point', [Infinity, 2]), shape('MultiPoint', [[0, 0], [NaN, 1]]),
    shape('LineString', [[0, 0], [2]]), shape('MultiPolygon', [[[[0, 0], [1, -Infinity]]]]),
    shape('MultiLineString', []), shape('MultiPoint', []), shape('Polygon', [[]]), shape('MultiPolygon', [[], [[]]])]) {
    assert.equal(geometryBounds(geometry), null);
    assert.equal(geometryIntersectsBounds(geometry, [-1, -1, 1, 1]), false);
  }
  for (const bounds of [null, [], [0, 0, NaN, 1], [0, 0, 1, Infinity], [2, 0, 1, 2], [0, 2, 2, 1], ['0', 0, 1, 1]]) {
    assert.equal(geometryIntersectsBounds(shape('Point', [0, 0]), bounds), false);
  }
});

test('finite coordinates whose differences overflow still intersect safely', () => {
  const huge = Number.MAX_VALUE;
  assert.equal(hit('LineString', [[-huge, 0], [huge, 0]], [-1, -1, 1, 1]), true);
  assert.equal(hit('LineString', [[-huge, -huge], [huge, huge]], [0, 0, 0, 0]), true);
  assert.equal(hit('Polygon', [ring(-huge, huge)], [-1, -1, 1, 1]), true);
});

test('large multipart geometry avoids argument-count limits and does not mutate inputs', () => {
  const points = Array.from({ length: 200000 }, (_, index) => [index, index % 2]);
  const geometry = shape('MultiLineString', [points, [[-10, -20], [-5, -15]]]);
  assert.deepEqual(geometryBounds(geometry), [-10, -20, 199999, 1]);
  assert.equal(geometryIntersectsBounds(geometry, [99999.25, 0, 99999.75, 1]), true);
  assert.deepEqual(points[0], [0, 0]);
  assert.deepEqual(points.at(-1), [199999, 1]);
});

test('Shapefile rings form multiple outer polygons rather than treating later exteriors as holes', () => {
  const outer = ring(), other = ring(20, 30);
  const result = polygonFromShapefileRings([outer, other]);
  assert.equal(result.type, 'MultiPolygon');
  assert.deepEqual(result.coordinates, [[outer], [other]]);
  assert.equal(geometryIntersectsBounds(result, [24, 24, 26, 26]), true);
  assert.equal(geometryIntersectsBounds(result, [12, 12, 18, 18]), false);
});

test('Shapefile holes bind to their nearest containing exterior even when listed first', () => {
  const outer = ring(), hole = ring(3, 7), other = ring(20, 30), otherHole = ring(24, 26);
  const result = polygonFromShapefileRings([otherHole, hole, other, outer]);
  assert.equal(result.type, 'MultiPolygon');
  assert.deepEqual(result.coordinates, [[other, otherHole], [outer, hole]]);
  assert.equal(geometryIntersectsBounds(result, [4, 4, 6, 6]), false);
  assert.equal(geometryIntersectsBounds(result, [24.5, 24.5, 25.5, 25.5]), false);
  assert.equal(geometryIntersectsBounds(result, [3, 4, 3, 6]), true);
});

test('Shapefile nesting preserves holes, islands and holes within islands at arbitrary depth', () => {
  const outer = ring(0, 20), hole = ring(2, 18), island = ring(4, 16), islandHole = ring(6, 14), core = ring(8, 12);
  const result = polygonFromShapefileRings([islandHole, island, core, hole, outer]);
  assert.equal(result.type, 'MultiPolygon');
  assert.deepEqual(result.coordinates, [[island, islandHole], [core], [outer, hole]]);
  for (const [coordinate, expected] of [[1, true], [3, false], [5, true], [7, false], [9, true]]) {
    assert.equal(geometryIntersectsBounds(result, [coordinate, coordinate, coordinate, coordinate]), expected);
  }
});

test('Shapefile topology and selection do not depend on ring order, direction or start vertex', () => {
  const parts = [ring(0, 20), ring(2, 18), ring(4, 16), ring(25, 30), ring(26, 29)];
  const selections = [[1, 1, 1, 1], [3, 3, 3, 3], [5, 5, 5, 5], [22, 22, 23, 23], [25, 25, 25, 25], [27, 27, 27, 27]];
  const expected = [true, false, true, false, true, false];
  const arrangements = [parts, [...parts].reverse(), [parts[2], parts[4], parts[0], parts[3], parts[1]]];
  for (const arrangement of arrangements) for (const reverse of [false, true]) {
    const shifted = arrangement.map(points => {
      const open = points.slice(0, -1);
      const moved = [...open.slice(2), ...open.slice(0, 2)];
      return reverse ? moved.reverse() : moved;
    });
    const result = polygonFromShapefileRings(shifted);
    assert.equal(result.type, 'MultiPolygon');
    assert.deepEqual(result.coordinates.map(polygon => polygon.length).sort(), [1, 2, 2]);
    assert.deepEqual(selections.map(bounds => geometryIntersectsBounds(result, bounds)), expected);
  }
});

test('containment uses real rings, not just nested bounding boxes', () => {
  const concave = [[0, 0], [6, 0], [6, 2], [2, 2], [2, 6], [0, 6]];
  const separate = ring(3, 5);
  const result = polygonFromShapefileRings([concave, separate]);
  assert.equal(result.type, 'MultiPolygon');
  assert.equal(geometryIntersectsBounds(result, [4, 4, 4, 4]), true);
});

test('boundary-touching inner vertices still establish strict containment via other samples', () => {
  const outer = ring(), diamond = [[5, 0], [10, 5], [5, 10], [0, 5]];
  const result = polygonFromShapefileRings([diamond, outer]);
  assert.equal(result.type, 'Polygon');
  assert.deepEqual(result.coordinates, [outer, diamond]);
  assert.equal(geometryIntersectsBounds(result, [5, 5, 5, 5]), false);
  assert.equal(geometryIntersectsBounds(result, [5, 0, 5, 0]), true);
});

test('ring conversion preserves source coordinates and rejects invalid values without dropping data', () => {
  const outer = ring(), hole = ring(3, 7), before = JSON.stringify([outer, hole]);
  assert.deepEqual(polygonFromShapefileRings([[], hole, outer]).coordinates, [outer, hole]);
  assert.equal(JSON.stringify([outer, hole]), before);
  assert.deepEqual(polygonFromShapefileRings([]), shape('Polygon', []));
  assert.deepEqual(polygonFromShapefileRings([[], []]), shape('Polygon', []));
  for (const invalid of [null, [null], [[[0, 0], [Infinity, 1]]], [[[0, 0], [1]]]]) {
    assert.throws(() => polygonFromShapefileRings(invalid), /Invalid Shapefile polygon/);
  }
});
