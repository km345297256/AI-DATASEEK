import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../src/components/ChatBox.vue', import.meta.url), 'utf8');

test('composer treats slash input as ordinary text', () => {
  assert.doesNotMatch(source, /slashMenuOpen|findSkillSlashTrigger|removeSkillSlashTrigger/);
  assert.match(source, /emit\('update:modelValue', target\.value\)/);
});

test('skills remain selectable from the add-content menu', () => {
  assert.match(source, /@click="toggleActionMenu"/);
  assert.match(source, /@click="toggleActionSkill\(skill\)"/);
});
