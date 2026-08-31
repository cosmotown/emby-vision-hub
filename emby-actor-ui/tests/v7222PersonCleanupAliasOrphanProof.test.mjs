import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(
  new URL('../src/components/PersonCleanupPage.vue', import.meta.url),
  'utf8',
);

test('alias orphan proof is explicitly readonly and never presented as cleanup', () => {
  assert.match(source, /Alias Orphan 只读证明/);
  assert.match(source, /本版本不会删除人物/);
  assert.match(source, /本版本不会删除人物/);
});

test('proof UI exposes progress, terminal state counts and persisted samples', () => {
  assert.match(source, /aliasProof\.checked_count/);
  assert.match(source, /aliasProof\.candidate_total/);
  assert.match(source, /verified_alias_orphan_count/);
  assert.match(source, /alias-proof-runs\/latest/);
  assert.match(source, /alias-proof-runs\/\$\{encodeURIComponent\(aliasProof\.value\.proof_id\)\}\/items/);
});

test('existing delete selection remains explicit orphan only', () => {
  assert.match(source, /row\.verification_status === 'orphan'/);
  assert.doesNotMatch(source, /verification_status === 'verified_alias_orphan'/);
});
