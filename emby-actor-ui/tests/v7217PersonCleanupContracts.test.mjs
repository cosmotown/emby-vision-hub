import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(
  new URL('../src/components/PersonCleanupPage.vue', import.meta.url),
  'utf8',
);

test('selection requires explicit orphan from the current snapshot generation', () => {
  assert.match(source, /row\.verification_status === 'orphan'/);
  assert.match(source, /row\.verification_snapshot_generation === snapshotGeneration\.value/);
  assert.match(source, /disabled: \(row\) => !isVerifiedOrphan\(row\)/);
  assert.doesNotMatch(source, /Boolean\(row\.last_checked_at && !row\.last_error\)/);
});

test('alias-only and snapshot-not-ready states are visibly fail closed', () => {
  assert.match(source, /identity_alias_only 始终受保护/);
  assert.match(source, /同身份别名：受保护/);
  assert.match(source, /protectionSnapshot\.state !== 'ready'/);
  assert.match(source, /保护快照尚未就绪，人物清理已锁定/);
});

test('one-click cleanup requires preview and explicit confirmation token', () => {
  assert.match(source, /cleanup-jobs\/preview/);
  assert.match(source, /cleanup-jobs\/\$\{encodeURIComponent\(jobId\)\}\/confirmation-token/);
  assert.match(source, /confirmation: safeCleanupConfirmation\.value/);
  assert.match(source, /确认删除已核验孤儿人物/);
  assert.match(source, /cleanup-jobs\/\$\{encodeURIComponent\(jobId\)\}\/confirm/);
  assert.match(source, /安全停止/);
});
