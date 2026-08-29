import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(
  new URL('../src/components/PersonCleanupPage.vue', import.meta.url),
  'utf8',
);

test('read-only scan shows persistent protected-alias phase progress', () => {
  assert.match(source, /readonlyScan\.checked_count/);
  assert.match(source, /readonlyScan\.candidate_total/);
  assert.match(source, /阶段 2：核验保护库别名人物/);
  assert.match(source, /本轮新增保护/);
  assert.match(source, /待人工复核/);
});

test('candidate list contract says protected aliases have been excluded', () => {
  assert.match(source, /只读核验确认的保护库别名人物/);
});

test('read-only scan UI does not grant delete eligibility', () => {
  assert.match(source, /row\.verification_status === 'orphan'/);
  assert.doesNotMatch(source, /readonlyScan[^\n]*verified_orphan/);
});
