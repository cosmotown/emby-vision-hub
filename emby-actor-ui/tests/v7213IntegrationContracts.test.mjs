import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const reviewList = await readFile(new URL('../src/components/ReviewList.vue', import.meta.url), 'utf8');
const generalSettings = await readFile(
  new URL('../src/components/settings/GeneralSettingsPage.vue', import.meta.url),
  'utf8',
);
const schedulerSettings = await readFile(
  new URL('../src/components/settings/SchedulerSettingsPage.vue', import.meta.url),
  'utf8',
);
const strmDiagnostics = await readFile(
  new URL('../src/components/settings/StrmIngestDiagnostics.vue', import.meta.url),
  'utf8',
);

test('ReviewList consumes backend target_item_id for status, recheck, and repair', () => {
  assert.match(reviewList, /row\?\.media_info_target\?\.target_item_id/);
  assert.match(reviewList, /items\/\$\{rowTargetId\(row\)\}\/status/);
  assert.match(reviewList, /items\/\$\{targetId\}\/recheck/);
  assert.match(reviewList, /items\/\$\{targetId\}\/repair/);
  assert.doesNotMatch(reviewList, /reason\.match|match\([^\n]*S\\d/);
});

test('global review action is wired only to the read-only recheck route', () => {
  const globalBlock = reviewList.slice(
    reviewList.indexOf('const recheckAllMediaInfo'),
    reviewList.indexOf('onMounted(() =>'),
  );
  assert.match(globalBlock, /review-targets/);
  assert.match(globalBlock, /\/recheck/);
  assert.doesNotMatch(globalBlock, /\/repair/);
  assert.match(globalBlock, /concurrency: 4/);
});

test('Shenyi JSON observation root reuses DirectoryPickerModal flow', () => {
  assert.match(generalSettings, /openDirectoryPicker\('mediainfo-json'\)/);
  assert.match(generalSettings, /directoryPickerTarget\.value === 'mediainfo-json'/);
  assert.match(generalSettings, /configModel\.value\.shenyi_mediainfo_json_root = path/);
  assert.match(generalSettings, /<DirectoryPickerModal/);
});

test('STRM Inventory audit is exposed as a manual-only gap check', () => {
  assert.match(schedulerSettings, /\/api\/tasks\/available\?context=chain/);
  assert.match(schedulerSettings, /\/api\/tasks\/available\?context=all/);
  assert.match(schedulerSettings, /task\.key === 'scan-monitor-folders'/);
  assert.match(schedulerSettings, /正常运行无需定期执行/);
  assert.match(strmDiagnostics, />STRM 查漏</);
  assert.match(strmDiagnostics, /不会递归 os\.walk/);
  assert.doesNotMatch(generalSettings, /path="monitor_full_scan_interval_hours"/);
  assert.doesNotMatch(generalSettings, /path="monitor_scan_lookback_days"/);
});
