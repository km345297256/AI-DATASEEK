import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const tiles = new URL('../node_modules/cesium/Build/Cesium/Assets/Textures/NaturalEarthII/', import.meta.url);

test('bundled Natural Earth tiles cover the full geographic pyramid without a remote dependency', () => {
  const manifest = readFileSync(new URL('tilemapresource.xml', tiles), 'utf8');
  assert.match(manifest, /<SRS>EPSG:4326<\/SRS>/);
  assert.match(manifest, /width="256" height="256"/);
  assert.deepEqual([...manifest.matchAll(/order="(\d+)"/g)].map(match => Number(match[1])), [0, 1, 2]);
  let count = 0;
  for (let level = 0; level <= 2; level++) {
    for (let x = 0; x < 2 ** (level + 1); x++) {
      for (let y = 0; y < 2 ** level; y++) {
        const image = readFileSync(new URL(`${level}/${x}/${y}.jpg`, tiles));
        assert.equal(image.readUInt16BE(0), 0xffd8);
        assert.equal(image.readUInt16BE(image.length - 2), 0xffd9);
        count++;
      }
    }
  }
  assert.equal(count, 42);
});

test('Cesium distribution retains the Natural Earth public-domain notice', () => {
  const license = readFileSync(new URL('../node_modules/cesium/LICENSE.md', import.meta.url), 'utf8');
  assert.match(license, /Public domain data from Natural Earth/);
  const build = readFileSync(new URL('../visualization-assets.ts', import.meta.url), 'utf8');
  assert.match(build, /\['Assets', 'Widgets', 'Workers', 'ThirdParty'\]/);
  assert.match(build, /cp\(resolve\(source, '\.\.\/\.\.\/LICENSE\.md'\), resolve\(target, 'LICENSE\.md'\)\)/);
});

test('offline TMS manifest and image middleware have explicit non-sniffed content types', () => {
  const build = readFileSync(new URL('../visualization-assets.ts', import.meta.url), 'utf8');
  assert.match(build, /xml: 'application\/xml'/);
  assert.match(build, /jpg: 'image\/jpeg'/);
  assert.match(build, /jpeg: 'image\/jpeg'/);
  assert.match(build, /X-Content-Type-Options', 'nosniff'/);
});
