import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(
  new URL('../src/components/PersonCleanupPage.vue', import.meta.url),
  'utf8',
);

test('stale run renders each persisted drift dimension separately', () => {
  for (const field of [
    'drift_protection',
    'drift_generation',
    'drift_normal_relationship',
    'drift_person',
    'drift_source_proof',
  ]) {
    assert.match(source, new RegExp(field));
  }
  assert.match(source, /Normal People relationship/);
  assert.match(source, /Source Alias Proof/);
});

test('drift UI explains fail-closed semantics and exposes only readonly samples', () => {
  assert.match(source, /这些变化导致本轮证据失败关闭；未降低任何安全条件/);
  assert.match(source, /关系变化样本（最多 20 条）/);
  assert.match(source, /Person 变化样本（最多 20 条）/);
  assert.doesNotMatch(source, /staleIndexRun[^\n]*DeletePerson/);
});

test('drift details include relationship person and protection summaries', () => {
  assert.match(source, /normal_relationship_drift_summary/);
  assert.match(source, /person_drift_summary/);
  assert.match(source, /protection_drift_summary/);
  assert.match(source, /source_proof_drift_summary/);
  assert.match(source, /protected provider identities/);
  assert.match(source, /root contract/);
});

test('legacy stale runs without final diagnostics are not shown as unchanged', () => {
  assert.match(source, /final_snapshot_generation !== null/);
  assert.match(source, /该历史记录生成于漂移诊断功能之前，无法判断具体漂移来源/);
  assert.match(source, /不会据此推断任何 snapshot 未变化/);
});

test('diagnostic summary failure stays visibly fail closed', () => {
  assert.match(source, /诊断摘要不可用/);
  assert.match(source, /原始 snapshot drift 仍按失败关闭处理/);
});
