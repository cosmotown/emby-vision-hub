import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(
  new URL('../src/components/PersonCleanupPage.vue', import.meta.url),
  'utf8',
);

test('Canary authentication is execution-only, bounded and visible without secrets', () => {
  assert.match(source, /预览仅只读/);
  assert.match(source, /最多一次管理员登录/);
  assert.match(source, /不自动重新登录/);
  assert.match(source, /admin_auth_attempts/);
  assert.match(source, /admin_session_verified/);
  assert.doesNotMatch(source, /staleDeleteCanaryJob\.(access_token|password|session_token)/);
});

test('Canary is a separate UI with a visible immutable backend limit', () => {
  assert.match(source, /Stable Stale Index Safe Delete Canary/);
  assert.match(source, /后端硬上限 100/);
  assert.match(source, /它不是 orphan 清理/);
  assert.match(source, /不提供全量删除、全选或 limit 覆盖/);
  assert.match(source, /stale-delete-canary\/preview/);
  assert.match(source, /\{ limit: 100 \}/);
});

test('Canary has a dedicated phrase and short-lived confirmation endpoint', () => {
  assert.match(source, /确认删除稳定陈旧索引 Canary 人物/);
  assert.match(source, /confirmation-token/);
  assert.match(source, /10 分钟后过期/);
  assert.match(source, /不会自动重放/);
});

test('Canary exposes boundary stop but no resume action', () => {
  assert.match(source, /在人物边界停止/);
  assert.match(source, /该任务不可 resume/);
  assert.doesNotMatch(source, /resumeStaleDeleteCanary/);
  assert.doesNotMatch(source, /继续 Canary/);
});

test('Canary copy preserves independent stable evidence semantics', () => {
  assert.match(source, /稳定证据 generation/);
  assert.match(source, /Canary ready/);
  assert.match(source, /确认删除/);
  assert.doesNotMatch(source, /stable_stale_index_signature[^\n]*verified_orphan/);
});

test('Canary exposes exclusions, ambiguity, final closure and preflight lifecycle', () => {
  assert.match(source, /same_name_excluded/);
  assert.match(source, /ambiguous_count/);
  assert.match(source, /final_verification\?\.person_delta_exact/);
  assert.match(source, /preflighting/);
  assert.match(source, /不可逆 Person 删除操作/);
  assert.match(source, /任一抽样人物预检拒绝则整次预览不可执行/);
});
