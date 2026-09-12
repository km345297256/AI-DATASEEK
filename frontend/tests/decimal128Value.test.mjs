import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {validateDecimal128} from '../src/visualizations/extended/decimal128Value.ts';

const oracle = JSON.parse(readFileSync(new URL('./browser/decimal128-oracle.json', import.meta.url), 'utf8'));
test('BID oracle is pinned native PyMongo rather than a frontend roundtrip', () => {
  assert.equal(oracle.version, '4.17.0');
  assert.match(oracle.source_sha256, /^[0-9a-f]{64}$/);
  assert.ok(oracle.cases.length > 2200);
  assert.ok(oracle.cases.some(value => value.error === 'Inexact'));
  assert.ok(oracle.cases.some(value => value.error === 'Overflow'));
});
test('BigInt decoder matches every native case and rejects a forged text', () => {
  for (const {name, bid, value} of oracle.cases) {
    if (value === null) {
      for (const text of ['0', '-0', 'NaN', 'Infinity', '1', '9'.repeat(40)]) {
        assert.equal(validateDecimal128(text, bid), false, `${name}: ${text}`);
      }
    } else {
      assert.equal(validateDecimal128(value, bid), true, name);
      assert.equal(validateDecimal128(`${value}0`, bid), false, name);
      assert.equal(validateDecimal128(` ${value}`, bid), false, name);
    }
  }
});
test('type, size and lowercase hex checks occur before BID parsing', () => {
  for (const value of [null, undefined, 0, true, {}, [], '', '9'.repeat(51), new String('NaN')]) {
    assert.equal(validateDecimal128(value, '0'.repeat(32)), false);
  }
  for (const bid of [null, undefined, 0, true, {}, [], '0'.repeat(31), '0'.repeat(33), 'A'.repeat(32),
    'g'.repeat(32), `${'0'.repeat(31)}\n`, `${'0'.repeat(32)}\n`, new String('0'.repeat(32))]) {
    assert.equal(validateDecimal128('NaN', bid), false);
  }
});
test('equivalent numeric values cannot erase signed zero, cohort or canonical spelling', () => {
  for (const [text, impostors] of [
    ['-0.0000', ['0.0000', '-0', '0', '-0E-4']],
    ['1.2300', ['1.23', '1.23000', '12300E-4']],
    ['NaN', ['-NaN', 'sNaN', 'nan']],
    ['Infinity', ['+Infinity', 'inf', '1E9999']],
  ]) {
    const {bid} = oracle.cases.find(value => value.name === `text:${text}`);
    assert.equal(validateDecimal128(text, bid), true);
    for (const value of impostors) assert.equal(validateDecimal128(value, bid), false);
  }
});
