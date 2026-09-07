import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { catalogText, datasetBytes, datasetMatches, datasetSourceGroup, publicSourceUrl } from '../src/utils/datasetCatalog.ts';

const dataset = { name: 'NOAA 气候数据', description: 'Monthly climate', domain: 'geoscience', tags: ['NetCDF'], data_center_name: 'NOAA', files: [{size:100},{size:20}], metadata: { publisher:'NOAA PSL', source_url:'https://psl.noaa.gov/data/', curated:true } };

test('source filters distinguish persistent local registrations and curated catalogs', () => {
  assert.equal(datasetSourceGroup(dataset), 'international');
  assert.equal(datasetSourceGroup({...dataset, metadata:{registration_kind:'owner_managed_directory'}}), 'local');
  for (const source_catalog of ['scidb', 'tpdc', 'chemdc', 'ngdc']) {
    assert.equal(datasetSourceGroup({...dataset, metadata:{curated:true, source_catalog}}), source_catalog);
  }
  assert.equal(datasetSourceGroup({...dataset, metadata:{curated:true, source_catalog:'untrusted'}}), 'international');
});

test('local setup persists registration before exploration, without browser path storage', async () => {
  const source = await readFile(new URL('../src/pages/DatasetSetupPage.vue', import.meta.url), 'utf8');
  assert.match(source, /await registerDataset\(/);
  assert.doesNotMatch(source, /createDatasetSubmission|localStorage|sessionStorage/);
  assert.ok(source.indexOf('await registerDataset(') < source.indexOf('await router.push('));
  assert.match(source, /已长期保存到数据集列表/);
});

test('catalog filters match live domain ids and case-insensitive metadata', () => {
  assert.equal(datasetMatches(dataset, 'geoscience', ' netcdf '), true);
  assert.equal(datasetMatches(dataset, 'tabular', ''), false);
  assert.equal(datasetMatches(dataset, '', 'CLIMATE'), true);
  assert.equal(datasetMatches({...dataset, domain:undefined}, 'general', ''), true);
  assert.equal(datasetBytes(dataset), 120);
  assert.equal(catalogText(dataset, 'curated'), '');
});

test('untrusted provenance links cannot execute code or embed credentials', () => {
  for (const value of ['javascript:alert(1)', 'data:text/html,test', 'file:///private/data', '//host/path', 'https://user:secret@example.com/']) assert.equal(publicSourceUrl(value), undefined);
  assert.equal(publicSourceUrl('https://psl.noaa.gov/data/'), 'https://psl.noaa.gov/data/');
  assert.equal(publicSourceUrl('http://chemdc.casdc.cn/home'), 'http://chemdc.casdc.cn/home');
  for (const value of ['http://localhost:7001/', 'http://example.com/', 'http://chemdc.casdc.cn.evil.test/', 'http://chemdc.casdc.cn:8080/', 'http://user:secret@chemdc.casdc.cn/']) assert.equal(publicSourceUrl(value), undefined);
});

test('dataset manager navigates by opaque id and does not persist host paths', async () => {
  const source = await readFile(new URL('../src/pages/DatasetManagementPage.vue', import.meta.url), 'utf8');
  assert.match(source, /getDomainPresetCatalog/);
  assert.match(source, /\/dataset\/seek\/\$\{encodeURIComponent\(item.dataset_id\)\}/);
  assert.doesNotMatch(source, /storage_directory|localStorage|sessionStorage|v-html/);
  assert.match(source, /不会删除原始文件/);
  const routes = await readFile(new URL('../src/router/index.ts', import.meta.url), 'utf8');
  assert.match(routes, /path: 'datasets',\s*alias: '\/datasets',\s*component: DatasetManagementPage/);
});

test('exploration links use a soft dedicated style without recoloring primary actions', async () => {
  const source = await readFile(new URL('../src/pages/DatasetManagementPage.vue', import.meta.url), 'utf8');
  assert.match(source, /class="explore-action flex-1">进入探查/);
  assert.match(source, /class="primary-action shrink-0"/);
  assert.match(source, /\.explore-action \{ background: #edf6f1; color: #2c7256;/);
  assert.match(source, /\.explore-action:focus-visible/);
  assert.match(source, /\.dark \.explore-action \{/);
});
