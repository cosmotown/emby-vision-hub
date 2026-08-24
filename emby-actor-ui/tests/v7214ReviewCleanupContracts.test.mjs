import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';


const reviewList = await readFile(
  new URL('../src/components/ReviewList.vue', import.meta.url),
  'utf8',
);


test('ReviewList exposes only fresh classified bulk cleanup actions', () => {
  assert.match(reviewList, /清理已恢复记录/);
  assert.match(reviewList, /清理失效历史记录/);
  assert.match(reviewList, /review-cleanup\/preview/);
  assert.match(reviewList, /review-cleanup\/execute/);
  assert.match(reviewList, /准备移出 \$\{count\} 条当前已恢复记录/);
  assert.doesNotMatch(reviewList, /\/api\/actions\/clear_review_items/);
  assert.doesNotMatch(reviewList, /重新处理所有/);
});


test('historical missing rows show unavailable layers and only removal action', () => {
  assert.match(reviewList, /historical_item_missing: '项目已不存在'/);
  assert.match(reviewList, /not_observable: '不可核对'/);
  assert.match(reviewList, /summary_status: 'historical_item_missing'/);
  assert.match(reviewList, /if \(historical\) \{/);
  assert.match(reviewList, /default: \(\) => '移出记录'/);
  const historicalBlock = reviewList.slice(
    reviewList.indexOf('if (historical) {'),
    reviewList.indexOf("actionButtons.push(\n        h(NButton", reviewList.indexOf('if (historical) {')),
  );
  assert.doesNotMatch(historicalBlock, /神医修复|重新处理|重新核对/);
});


test('historical reason and current status are rendered separately', () => {
  assert.match(reviewList, /title: '历史原因'/);
  assert.match(reviewList, /当前状态: \$\{summaryStatusLabel\(status\.summary_status\)\}/);
  assert.match(reviewList, /ready: '已恢复'/);
  assert.match(reviewList, /default: \(\) => mediaStatus\?\.summary_status === 'ready' \? '移出列表' : '移出记录'/);
});


test('unresolved Episode target cannot invoke exact recheck, repair, or legacy reprocess', () => {
  assert.match(reviewList, /disabled: !rowTargetId\(row\) \|\| globalRecheck/);
  assert.match(reviewList, /disabled: !repairFeatureEnabled\.value \|\| !mediaStatus\?\.repair_eligible/);
  assert.match(reviewList, /disabled: !rowTargetId\(row\) \|\| loadingAction\.value\[row\.item_id\]/);
});
