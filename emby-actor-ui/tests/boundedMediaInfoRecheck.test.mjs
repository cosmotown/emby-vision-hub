import assert from 'node:assert/strict';
import test from 'node:test';

import {
  collectUniqueReviewTargets,
  runBoundedReadOnlyRecheck,
} from '../src/utils/boundedMediaInfoRecheck.js';

const target = (source, item) => ({ source_item_id: source, target: { target_item_id: item } });

test('deduplicates explicit Episode targets across review rows', () => {
  assert.deepEqual(collectUniqueReviewTargets([
    target('series-1', 'episode-8'),
    target('episode-row', 'episode-8'),
    target('series-2', 'episode-9'),
    { source_item_id: 'unresolved', target: { target_item_id: null } },
  ]), [
    { targetId: 'episode-8', sourceIds: ['series-1', 'episode-row'] },
    { targetId: 'episode-9', sourceIds: ['series-2'] },
  ]);
});

test('bounds concurrency and invokes read-only recheck once per target', async () => {
  let active = 0;
  let peak = 0;
  const paths = [];
  const entries = Array.from({ length: 12 }, (_, index) => target(`source-${index}`, `episode-${index}`));
  const result = await runBoundedReadOnlyRecheck({
    entries,
    concurrency: 3,
    requestRecheck: async (itemId) => {
      paths.push(`/api/media-info/items/${itemId}/recheck`);
      active += 1;
      peak = Math.max(peak, active);
      await new Promise(resolve => setTimeout(resolve, 2));
      active -= 1;
      return { state: 'ready' };
    },
  });
  assert.equal(peak, 3);
  assert.equal(paths.length, 12);
  assert.ok(paths.every(path => path.endsWith('/recheck')));
  assert.ok(paths.every(path => !path.includes('/repair')));
  assert.deepEqual(result, { total: 12, completed: 12, succeeded: 12, failed: 0, cancelled: false });
});

test('cancellation stops dispatch and ignores stale results', async () => {
  const controller = new AbortController();
  let calls = 0;
  let committed = 0;
  const promise = runBoundedReadOnlyRecheck({
    entries: Array.from({ length: 20 }, (_, index) => target(`source-${index}`, `episode-${index}`)),
    concurrency: 2,
    signal: controller.signal,
    requestRecheck: async () => {
      calls += 1;
      if (calls === 2) controller.abort();
      await new Promise(resolve => setTimeout(resolve, 2));
      return { state: 'ready' };
    },
    onResult: () => { committed += 1; },
  });
  const result = await promise;
  assert.equal(calls, 2);
  assert.equal(committed, 0);
  assert.equal(result.cancelled, true);
});

test('failed rechecks are counted without retry', async () => {
  const calls = new Map();
  const result = await runBoundedReadOnlyRecheck({
    entries: [target('a', 'episode-a'), target('b', 'episode-b')],
    concurrency: 2,
    requestRecheck: async (itemId) => {
      calls.set(itemId, (calls.get(itemId) || 0) + 1);
      throw new Error('readback failed');
    },
  });
  assert.deepEqual([...calls.values()], [1, 1]);
  assert.deepEqual(result, { total: 2, completed: 2, succeeded: 0, failed: 2, cancelled: false });
});
