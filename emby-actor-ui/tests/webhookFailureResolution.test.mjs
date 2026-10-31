import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../src/components/settings/WebhookQueueDiagnostics.vue', import.meta.url), 'utf8');

test('historical errors are separate from recovered and manually archived states', () => {
  assert.match(source, /历史错误：/);
  assert.match(source, /recovered: \{ label: '已恢复'/);
  assert.match(source, /archived: \{ label: '已归档'/);
  assert.match(source, /不代表原事件执行成功/);
  assert.match(source, /event\.resolution_json\?\.event_id/);
});

test('archive is an explicit confirmation action and never invokes retry', () => {
  assert.match(source, /<n-popconfirm[\s\S]*positive-text="确认正常并归档"/);
  assert.match(source, /请先确认该事件涉及的媒体、集数、详情及播放均正常/);
  const archive = source.slice(source.indexOf('const archiveEvent ='), source.indexOf('onMounted(loadEvents)'));
  assert.match(archive, /\/archive`, \{ confirmed_media_ok: true \}/);
  assert.doesNotMatch(archive, /\/retry|retryEvent\(/);
  assert.match(archive, /未重放任务/);
});

test('only failed rows offer archive or retry and repeated clicks are guarded', () => {
  assert.match(source, /<n-space v-if="event.status === 'failed'">/);
  assert.match(source, /retryingId\.value !== null \|\| archivingId\.value !== null/);
  assert.match(source, /label: '待处理失败'/);
  assert.match(source, /summary\.value\.recovered_count/);
  assert.match(source, /summary\.value\.archived_count/);
});
