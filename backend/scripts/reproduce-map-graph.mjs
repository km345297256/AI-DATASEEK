import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

// Trusted, deterministic conversion of downloaded data only; no external code,
// subprocesses, network fetches, or mutation of original inputs.
// Usage: node reproduce-map-graph.mjs [cache-root] [--write]
// Default only compares expected derived bytes against existing files.
const here = path.dirname(fileURLToPath(import.meta.url));
const cacheRoot = path.resolve(process.argv.slice(2).find(a => a !== '--write') || here);
const write = process.argv.includes('--write');
const readJson = p => JSON.parse(fs.readFileSync(p, 'utf8'));
function checkOrWrite(relative, text) {
  const destination = path.join(cacheRoot, relative);
  const bytes = Buffer.from(text + '\n', 'utf8');
  if (write) fs.writeFileSync(destination, bytes, { flag: 'wx' });
  else assert.deepEqual(fs.readFileSync(destination), bytes, 'Derived bytes differ: ' + relative);
  return { path: relative, bytes: bytes.length };
}

const mapPrefix = 'viz-usgs-2024-m6-earthquakes';
const map = readJson(path.join(cacheRoot, mapPrefix, 'usgs-2024-m6-original.geojson'));
assert.equal(map.type, 'FeatureCollection');
assert.equal(map.features.length, 99);
assert(!map.crs);
if (map.bbox) {
  assert.equal(map.bbox.length, 6);
  map.source_bbox = map.bbox;
  map.bbox = [map.bbox[0], map.bbox[1], map.bbox[3], map.bbox[4]];
}
for (const feature of map.features) {
  assert.equal(feature.geometry.type, 'Point');
  assert.equal(feature.geometry.coordinates.length, 3);
  assert(feature.geometry.coordinates.every(Number.isFinite));
  assert(!('depth_km' in feature.properties) && !('name' in feature.properties));
  feature.properties.depth_km = feature.geometry.coordinates[2];
  feature.properties.name = feature.properties.title;
  feature.geometry.coordinates = feature.geometry.coordinates.slice(0, 2);
}
map.coordinate_normalization = 'Original USGS third coordinate is depth in km, not RFC7946 ellipsoidal height. It is preserved as properties.depth_km; XY coordinates and all original properties are unchanged. Original file is retained.';
const results = [checkOrWrite(mapPrefix + '/usgs-2024-m6-wgs84-2d.geojson', JSON.stringify(map, null, 2))];

const graphPrefix = 'viz-mangal-greenland-pollination-908';
const nodes = readJson(path.join(cacheRoot, graphPrefix, 'nodes.json')).sort((a,b) => a.id-b.id);
const edges = readJson(path.join(cacheRoot, graphPrefix, 'interactions.json')).sort((a,b) => a.id-b.id);
assert.equal(nodes.length, 43); assert.equal(edges.length, 63);
const nodeIds = new Set(nodes.map(n => n.id)), pairs = new Set();
assert.equal(nodeIds.size, nodes.length);
for (const edge of edges) {
  assert.equal(edge.direction, 'directed');
  assert(nodeIds.has(edge.node_from) && nodeIds.has(edge.node_to) && edge.node_from !== edge.node_to);
  assert(Number.isFinite(edge.value));
  const pair = edge.node_from + '/' + edge.node_to;
  assert(!pairs.has(pair)); pairs.add(pair);
}
const escape = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const graph = [
  '<?xml version="1.0" encoding="UTF-8"?>',
  '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
  '<key id="label" for="node" attr.name="label" attr.type="string"/>',
  '<key id="group" for="node" attr.name="group" attr.type="string"/>',
  '<key id="relation" for="edge" attr.name="label" attr.type="string"/>',
  '<key id="weight" for="edge" attr.name="weight" attr.type="double"/>',
  '<graph id="mangal908" edgedefault="directed">',
  ...nodes.map(n => '<node id="m'+n.id+'"><data key="label">'+escape(n.original_name)+'</data><data key="group">'+escape(n.node_level)+'</data></node>'),
  ...edges.map(e => '<edge id="i'+e.id+'" source="m'+e.node_from+'" target="m'+e.node_to+'"><data key="relation">'+escape(e.type)+'</data><data key="weight">'+e.value+'</data></edge>'),
  '</graph>', '</graphml>'
].join('\n');
results.push(checkOrWrite(graphPrefix + '/lundgren-olesen-2005-network-908.graphml', graph));
process.stdout.write(JSON.stringify({ status:'passed', mode:write?'write-derived-only':'read-only-reproduction-check', results }) + '\n');
