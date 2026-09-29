import assert from 'node:assert/strict';
import test from 'node:test';
import { createAgentProfileDirectory, AGENT_PROFILE_STORAGE_KEY } from '../src/utils/agentProfileDirectory.ts';
import { useAnalysisSession } from '../src/composables/useAnalysisSession.ts';

const profile = (id, extra = {}) => ({ id, name: id, ...extra });
function harness(savedId = null) {
  const reads = [], values = new Map(savedId ? [[AGENT_PROFILE_STORAGE_KEY, savedId]] : []);
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  const directory = createAgentProfileDirectory(() => new Promise((resolve, reject) => reads.push({ resolve, reject })), storage);
  return { ...directory, reads, values };
}
const start = async (h, options) => { const pending = h.refreshProfiles(options); await Promise.resolve(); return { pending }; };

test('overlapping readers share one request and the same settlement', async () => {
  const h = harness('old');
  const a = h.refreshProfiles(), b = h.refreshProfiles();
  assert.equal(a, b); await Promise.resolve(); assert.equal(h.reads.length, 1);
  h.reads[0].resolve([profile('old')]); await a;
  assert.equal(h.selectedProfile.value.id, 'old');
});

test('old directory response cannot clear a newly created and selected profile', async () => {
  const h = harness(); const a = await start(h);
  h.setSelectedProfile(profile('new'));
  const b = await start(h);
  h.reads[1].resolve([profile('new')]); await b.pending;
  h.reads[0].resolve([]); await a.pending;
  assert.equal(h.selectedProfileId.value, 'new');
  assert.equal(h.selectedProfile.value.id, 'new');
  assert.deepEqual(h.profiles.value.map(item => item.id), ['new']);
  assert.equal(h.values.get(AGENT_PROFILE_STORAGE_KEY), 'new');
});

test('selection alone invalidates an older read, even without a second request', async () => {
  const h = harness(); const a = await start(h);
  h.setSelectedProfile(profile('new', { model_name: 'chosen-model' }));
  h.reads[0].resolve([]); await a.pending;
  assert.equal(h.selectedProfileId.value, 'new');
  assert.equal(h.selectedProfile.value.model_name, 'chosen-model');
});

test('save-triggered force refresh supersedes a pre-save single flight', async () => {
  const h = harness('same'); const a = await start(h); const b = await start(h, { force: true });
  h.reads[1].resolve([profile('same', { model_name: 'new-model' })]); await b.pending;
  h.reads[0].resolve([profile('same', { model_name: 'old-model' })]); await a.pending;
  assert.equal(h.selectedProfile.value.model_name, 'new-model');
});

test('superseded cleanup does not clear a newer in-flight request', async () => {
  const h = harness(); const a = await start(h); const b = await start(h, { force: true });
  h.reads[0].resolve([]); await a.pending;
  assert.equal(h.refreshProfiles(), b.pending);
  h.reads[1].resolve([]); await b.pending; assert.equal(h.reads.length, 2);
});

test('failure preserves last good catalog and selection, releases the request, and permits retry', async () => {
  const h = harness('kept'); const first = await start(h);
  h.reads[0].resolve([profile('kept')]); await first.pending;
  const failed = await start(h); h.reads[1].reject(new Error('offline'));
  await assert.rejects(failed.pending, /offline/);
  assert.equal(h.selectedProfileId.value, 'kept'); assert.equal(h.selectedProfile.value.name, 'kept');
  assert.deepEqual(h.profiles.value.map(item => item.id), ['kept']);
  assert.equal(h.values.get(AGENT_PROFILE_STORAGE_KEY), 'kept');
  const retry = await start(h); h.reads[2].resolve([profile('kept', { name: 'Updated' })]); await retry.pending;
  assert.equal(h.selectedProfile.value.name, 'Updated');
});

test('a successful fresh read still observes a real deletion', async () => {
  const h = harness(); h.setSelectedProfile(profile('deleted')); const a = await start(h);
  h.reads[0].resolve([profile('other')]); await a.pending;
  assert.equal(h.selectedProfileId.value, null); assert.equal(h.selectedProfile.value, null);
  assert.equal(h.values.has(AGENT_PROFILE_STORAGE_KEY), false);
  assert.deepEqual(h.profiles.value.map(item => item.id), ['other']);
});

test('returning to the default selection cannot be undone by a pending directory read', async () => {
  const h = harness('old'); const a = await start(h); h.setSelectedProfile(null);
  h.reads[0].resolve([profile('old')]); await a.pending;
  assert.equal(h.selectedProfileId.value, null); assert.equal(h.selectedProfile.value, null);
  assert.equal(h.values.size, 0);
});

test('a late failed read cannot release or overwrite a newer request', async () => {
  const h = harness(); const a = await start(h); const b = await start(h, { force: true });
  h.reads[0].reject(new Error('old failure')); await assert.rejects(a.pending, /old failure/);
  assert.equal(h.refreshProfiles(), b.pending);
  h.reads[1].resolve([profile('fresh')]); await b.pending;
  assert.deepEqual(h.profiles.value.map(item => item.id), ['fresh']);
});

test('only the selected opaque ID is persisted, never profile fields', () => {
  const h = harness(); h.setSelectedProfile(profile('id', { api_key: 'synthetic-private', system_prompt: 'synthetic-prompt' }));
  assert.deepEqual([...h.values], [[AGENT_PROFILE_STORAGE_KEY, 'id']]);
});

test('a new analysis still submits the chosen profile after old catalog settlement', async () => {
  const h = harness(); const stale = await start(h); h.setSelectedProfile(profile('chosen'));
  h.reads[0].resolve([]); await stale.pending;
  const requests = [];
  const session = useAnalysisSession({ api: {
    createClientMessageId: () => 'synthetic-input',
    chatWithSession: async (...args) => { requests.push(args); return () => {}; },
  } });
  session.sessionId.value = 'synthetic-session';
  await session.send({ message: 'Analyze synthetic data', agentProfileId: h.selectedProfileId.value });
  assert.equal(requests[0][7], 'chosen');
  session.dispose();
});
