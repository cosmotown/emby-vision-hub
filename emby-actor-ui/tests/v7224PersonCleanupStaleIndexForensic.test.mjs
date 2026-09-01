import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(
  new URL('../src/components/PersonCleanupPage.vue', import.meta.url),
  'utf8',
);

test('stale index forensic is presented as readonly evidence, never deletion', () => {
  assert.match(source, /Stale Index 只读取证/);
  assert.match(source, /不代表本版本允许删除人物/);
  assert.match(source, /verified_stale_index_signature/);
  assert.match(source, /stable_stale_index_signature/);
  assert.doesNotMatch(source, /verification_status === 'verified_stale_index_signature'/);
  assert.doesNotMatch(source, /verification_status === 'stable_stale_index_signature'/);
});

test('UI exposes persistent progress, signals and paged samples', () => {
  assert.match(source, /staleIndexRun\.checked_count/);
  assert.match(source, /staleIndexRun\.candidate_total/);
  assert.match(source, /verified_signature_count/);
  assert.match(source, /stable_signature_count/);
  assert.match(source, /identity_signals/);
  assert.match(source, /people_signals/);
  assert.match(source, /stale-index-runs\/latest/);
  assert.match(source, /stale-index-runs\/\$\{encodeURIComponent\(staleIndexRun\.value\.run_id\)\}\/items/);
});

test('existing delete selection remains explicit orphan only', () => {
  assert.match(source, /row\.verification_status === 'orphan'/);
  assert.doesNotMatch(source, /verified_stale_index_signature.*DeletePerson/s);
  assert.doesNotMatch(source, /stable_stale_index_signature.*DeletePerson/s);
});
