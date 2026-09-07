import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { apiClient } from '../src/api/client.ts';
import { getDomainPresetCatalog } from '../src/api/domainPreset.ts';
import { createRuntimePresetProfile, updateAgentProfile } from '../src/api/agentProfile.ts';
import { copyAgentToolRuntime, LEGACY_PROFILE_TOOL_RUNTIME, sameAgentToolRuntime } from '../src/utils/agentToolRuntime.ts';


function adapterResponse(config, data) {
  return {
    config,
    data,
    headers: {},
    request: {},
    status: 200,
    statusText: 'OK',
  };
}


const runtime = {
  preset_id: 'geoscience',
  selection_mode: 'on_demand',
  code_mode_enabled: true,
  domain_subagents_enabled: false,
};

const profile = {
  id: 'profile-1',
  name: 'Geoscience',
  model_name: 'configured-model',
  tool_runtime: runtime,
};

const catalog = {
  presets: [{
    id: 'geoscience',
    name: 'Geoscience',
    description: 'Geospatial and Earth science analysis',
    plugin_ids: ['geospatial', 'netcdf'],
    initial_tools: ['dataset_quicklook'],
    available_tool_count: 18,
    initial_tool_count: 1,
  }],
  defaults: {
    preset_id: 'general',
    selection_mode: 'on_demand',
    code_mode_enabled: false,
    domain_subagents_enabled: false,
  },
  catalog_revision: 'revision-1',
};


test('preset catalog and profile runtime APIs keep their narrow contracts', async () => {
  const originalAdapter = apiClient.defaults.adapter;
  const calls = [];
  apiClient.defaults.adapter = async (config) => {
    calls.push({
      method: config.method,
      url: config.url,
      body: typeof config.data === 'string' ? JSON.parse(config.data) : config.data,
    });
    const data = config.url === '/plugins/presets' ? catalog : profile;
    return adapterResponse(config, { code: 0, msg: 'success', data });
  };

  try {
    assert.deepEqual(await getDomainPresetCatalog(), catalog);
    assert.deepEqual(await updateAgentProfile('profile-1', { tool_runtime: runtime }), profile);
    assert.deepEqual(await createRuntimePresetProfile({ name: 'Geoscience', tool_runtime: runtime }), profile);
    assert.deepEqual(calls, [
      { method: 'get', url: '/plugins/presets', body: undefined },
      { method: 'put', url: '/agent-profiles/profile-1', body: { tool_runtime: runtime } },
      { method: 'post', url: '/agent-profiles/runtime-preset', body: { name: 'Geoscience', tool_runtime: runtime } },
    ]);
  } finally {
    apiClient.defaults.adapter = originalAdapter;
  }
});


test('missing tool runtime is displayed as legacy all without mutating defaults', () => {
  const normalized = copyAgentToolRuntime(undefined);
  assert.deepEqual(normalized, {
    preset_id: 'general',
    selection_mode: 'all',
    code_mode_enabled: false,
    domain_subagents_enabled: false,
  });
  normalized.preset_id = 'tabular';
  assert.equal(LEGACY_PROFILE_TOOL_RUNTIME.preset_id, 'general');
  assert.equal(sameAgentToolRuntime(undefined, { ...LEGACY_PROFILE_TOOL_RUNTIME }), true);
  assert.equal(sameAgentToolRuntime(undefined, runtime), false);
});


test('settings and plugins reuse existing surfaces and make experiments explicit', async () => {
  const [settingsDialog, runtimeSettings, pluginsPage] = await Promise.all([
    readFile(new URL('../src/components/settings/SettingsDialog.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/components/settings/AgentProfileRuntimeSettings.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/pages/PluginsPage.vue', import.meta.url), 'utf8'),
  ]);

  assert.match(settingsDialog, /#agent-profiles/);
  assert.match(runtimeSettings, /draft\.preset_id/);
  assert.match(runtimeSettings, /draft\.selection_mode/);
  assert.match(runtimeSettings, /draft\.code_mode_enabled/);
  assert.match(runtimeSettings, /draft\.domain_subagents_enabled/);
  assert.match(runtimeSettings, /createRuntimePresetProfile/);
  assert.match(runtimeSettings, /updateAgentProfile\(profile!\.id, \{ tool_runtime: toolRuntime \}\)/);
  assert.match(runtimeSettings, /Experimental/);
  assert.match(runtimeSettings, /never copies an API key/);
  assert.doesNotMatch(runtimeSettings, /localStorage|sessionStorage|api_key|api_base/);
  assert.match(pluginsPage, /key: 'presets', label: t\('Domain Presets'\)/);
  assert.match(pluginsPage, /preset\.initial_tools/);
  assert.match(pluginsPage, /preset\.plugin_ids/);
  assert.match(pluginsPage, /All mode means all tools in this preset/);
});


test('preset and selector entries open the existing editor without enabling experiments', async () => {
  const [plugins, selector, settings] = await Promise.all([
    readFile(new URL('../src/pages/PluginsPage.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/components/AgentSelector.vue', import.meta.url), 'utf8'),
    readFile(new URL('../src/components/settings/AgentProfileRuntimeSettings.vue', import.meta.url), 'utf8'),
  ]);
  assert.match(plugins, /@click="openSettingsDialog\('agent-profiles'\)"/);
  assert.match(plugins, /Configure Agent \/ Enable pilots/);
  assert.match(plugins, /settings !== 'agent-profiles'/);
  assert.match(plugins, /delete query\.settings/);
  assert.match(selector, /function manageProfiles\(\)[\s\S]*?openSettingsDialog\('agent-profiles'\)/);
  assert.ok(settings.indexOf('v-model="draft.code_mode_enabled"') < settings.indexOf('Default without a profile'));
  assert.ok(settings.indexOf('v-model="draft.domain_subagents_enabled"') < settings.indexOf('Preset scope and tool counts'));
  assert.match(settings, /function useProfileInNewChat\(\)[\s\S]*?hasChanges\.value\) return;/);
  assert.match(settings, /setSelectedProfile\(selectedProfile\.value\);\s*closeSettingsDialog\(\);\s*void router.push\('\/'\)/);
  assert.doesNotMatch(plugins + selector, /code_mode_enabled\s*[:=]\s*true|domain_subagents_enabled\s*[:=]\s*true/);
});
