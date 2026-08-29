import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

import {
  buildPersonCleanupPreviewRows,
  personCleanupPreviewLabel,
  personCleanupPreviewPercentage,
} from '../src/utils/personCleanupPreview.js';


const source = fs.readFileSync(
  new URL('../src/components/PersonCleanupPage.vue', import.meta.url),
  'utf8',
);

const distribution = {
  verified_orphan: 712,
  identity_alias_only: 18000,
  people_unavailable: 2500,
  invalid_response: 600,
  connection_failed: 300,
  linked: 200,
  protected_library_alias: 150,
  protected_library_unverifiable: 100,
  protected_id: 50,
  protected_name: 40,
  protected_provider_identity: 30,
  future_unknown_state: 32,
};

test('22714 persisted preview states remain complete with correct percentages', () => {
  const states = Object.entries(distribution).map(([status, count]) => ({ status, count }));
  const rows = buildPersonCleanupPreviewRows({ candidate_total: 22714, states });

  assert.equal(rows.reduce((sum, row) => sum + row.count, 0), 22714);
  assert.equal(distribution.verified_orphan, 712);
  assert.equal(22714 - distribution.verified_orphan, 22002);
  assert.equal(personCleanupPreviewPercentage(712, 22714), '3.13%');
  assert.equal(personCleanupPreviewPercentage(22002, 22714), '96.87%');
  assert.equal(
    rows.find((row) => row.status === 'future_unknown_state').label,
    '其他：future_unknown_state',
  );
  assert.equal(rows.find((row) => row.status === 'verified_orphan').sample_available, false);
  assert.equal(rows.find((row) => row.status === 'identity_alias_only').sample_available, true);
});

test('known safety states have explicit user-facing labels', () => {
  assert.equal(personCleanupPreviewLabel('identity_alias_only'), '同身份别名');
  assert.equal(personCleanupPreviewLabel('people_unavailable'), 'People 无法完整核验');
  assert.equal(personCleanupPreviewLabel('protected_library_alias'), '保护库 alias');
  assert.equal(personCleanupPreviewLabel('protected_library_unverifiable'), '保护库不可核验');
  assert.equal(personCleanupPreviewLabel('protected_provider_identity'), '保护人物外部身份');
});

test('page loads historical job summary and lazily pages persisted samples', () => {
  assert.match(source, /核验结果明细/);
  assert.match(source, /“核验失败”不等于“不是幽灵人物”/);
  assert.match(source, /当前证据不足以授予删除资格/);
  assert.match(source, /cleanup-jobs\/latest/);
  assert.match(source, /cleanup-jobs\/\$\{encodeURIComponent\(safeCleanupJob\.value\.job_id\)\}\/preview-items/);
  assert.match(source, /page_size: previewSamplesPageSize/);
  assert.match(source, /v-if="row\.sample_available"/);
  assert.match(source, /@click="openPreviewSamples\(row\)"/);
  assert.match(source, /providerIdText\(row\.provider_ids_json\)/);
  assert.match(source, /fetchLatestSafeCleanupJob\(\)/);
});
