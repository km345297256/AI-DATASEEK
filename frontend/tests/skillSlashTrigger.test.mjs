import assert from 'node:assert/strict';
import test from 'node:test';

import { findSkillSlashTrigger, removeSkillSlashTrigger } from '../src/utils/skillSlashTrigger.ts';

test('resolves an empty Skill query at the start of the composer', () => {
  assert.deepEqual(findSkillSlashTrigger('/', 1), {
    query: '',
    range: { start: 0, end: 1 },
  });
});

test('resolves a Skill query after whitespace', () => {
  assert.deepEqual(findSkillSlashTrigger('分析一下 /climate', 13), {
    query: 'climate',
    range: { start: 5, end: 13 },
  });
});

test('does not treat embedded URL and word slashes as Skill commands', () => {
  assert.equal(findSkillSlashTrigger('https://example.com', 8), null);
  assert.equal(findSkillSlashTrigger('abc/', 4), null);
  assert.equal(findSkillSlashTrigger('分析/a', 4), null);
});

test('does not open a Skill menu during IME composition', () => {
  assert.equal(findSkillSlashTrigger('/', 1, true), null);
});

test('removes only the active slash query after Skill selection', () => {
  assert.deepEqual(
    removeSkillSlashTrigger('分析一下 /climate 后续内容', { start: 5, end: 13 }),
    { value: '分析一下  后续内容', cursor: 5 },
  );
});
