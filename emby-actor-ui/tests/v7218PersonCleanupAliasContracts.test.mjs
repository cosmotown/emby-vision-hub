import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source = fs.readFileSync(
  new URL('../src/components/PersonCleanupPage.vue', import.meta.url),
  'utf8',
);

test('protected-library alias outcomes are explicit protected states', () => {
  assert.match(source, /protected_library_alias: '受保护媒体库别名人物'/);
  assert.match(source, /protected_library_unverifiable: '受保护媒体库人物明细不可核验'/);
  assert.match(source, /仅以其他 Person 身份关联受保护媒体库作品/);
  assert.match(source, /受保护媒体库作品的 People 明细无法完整核验/);
});

test('completed protected-library verification revokes the candidate row', () => {
  assert.match(source, /if \(response\.data\.candidate_removed\)/);
  assert.match(source, /candidates\.value = candidates\.value\.filter/);
  assert.match(source, /item\.person_id !== row\.person_id/);
});

test('selection and deletion remain limited to explicit current-generation orphans', () => {
  assert.match(source, /row\.verification_status === 'orphan'/);
  assert.match(source, /disabled: \(row\) => !isVerifiedOrphan\(row\)/);
  assert.doesNotMatch(source, /protected_library_alias[^\n]*orphan/);
  assert.doesNotMatch(source, /protected_library_unverifiable[^\n]*orphan/);
});
