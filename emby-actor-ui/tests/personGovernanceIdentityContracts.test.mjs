import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const editor = fs.readFileSync(new URL('../src/components/MediaEditPage.vue', import.meta.url), 'utf8');

test('manual actor edits submit the frozen instance ID rather than a name-derived target', () => {
  assert.match(editor, /emby_person_id:\s*actor\.emby_person_id/);
});

test('manual actor edits retain the backend provider identity snapshot', () => {
  assert.match(editor, /provider_identity_snapshot:\s*actor\.provider_identity_snapshot/);
});

test('new actor UI entries do not fabricate an existing Emby Person ID', () => {
  assert.match(editor, /emby_person_id:\s*null/);
});
