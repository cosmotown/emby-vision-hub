import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const page = fs.readFileSync(
  new URL('../src/components/ReleasesPage.vue', import.meta.url),
  'utf8',
);
const store = fs.readFileSync(
  new URL('../src/stores/app.js', import.meta.url),
  'utf8',
);

test('update starts through POST and cannot accept an image or container target', () => {
  assert.match(page, /axios\.post\('\/api\/system\/update\/start'\)/);
  assert.doesNotMatch(page, /update\/start'\s*,\s*\{/);
  assert.doesNotMatch(page, /new EventSource/);
});

test('persistent transaction id drives reconnect polling', () => {
  assert.match(page, /evhUpdateTransactionId/);
  assert.match(page, /\/api\/system\/update\/status\/\$\{updateTransactionId\.value\}/);
  assert.match(page, /服务正在重启或暂时不可达，正在重新连接更新事务/);
});

test('network disconnect is not treated as update success', () => {
  assert.doesNotMatch(page, /连接中断[\s\S]{0,180}isUpdateFinished\.value\s*=\s*true/);
  assert.match(page, /SUCCESS: '更新完成，所有提交条件均已通过。'/);
  assert.match(page, /ROLLED_BACK:/);
  assert.match(page, /FAILED:/);
  assert.match(page, /transaction\.last_error/);
});

test('stable version discovery uses backend stable release and semantic comparison', () => {
  assert.match(store, /latest_stable_version/);
  assert.match(store, /\^v\?\(0\|\[1-9\]\\d\*\)/);
  assert.doesNotMatch(store, /normalizedLatest\s*!==\s*normalizedCurrent/);
});
