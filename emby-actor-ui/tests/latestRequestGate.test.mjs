import assert from 'node:assert/strict';
import test from 'node:test';

import { createLatestRequestGate } from '../src/utils/latestRequestGate.js';

test('older status cannot overwrite a newer ready recheck', () => {
  const gate = createLatestRequestGate();
  const status = gate.begin('episode-1');
  const recheck = gate.begin('episode-1');
  let value = null;
  assert.equal(recheck.commit(() => { value = 'ready'; }), true);
  assert.equal(status.commit(() => { value = 'unknown'; }), false);
  assert.equal(value, 'ready');
  assert.equal(status.signal.aborted, true);
});

test('repair generation invalidates an older recheck', () => {
  const gate = createLatestRequestGate();
  const recheck = gate.begin('episode-1');
  const repair = gate.begin('episode-1');
  let value = 'repairing';
  repair.commit(() => { value = 'ready'; });
  recheck.commit(() => { value = 'incomplete'; });
  assert.equal(value, 'ready');
});

test('dispose prevents component updates', () => {
  const gate = createLatestRequestGate();
  const request = gate.begin('episode-1');
  let updated = false;
  gate.dispose();
  assert.equal(request.commit(() => { updated = true; }), false);
  assert.equal(updated, false);
  assert.equal(request.signal.aborted, true);
});

test('different items have independent generations', () => {
  const gate = createLatestRequestGate();
  const first = gate.begin('episode-1');
  const second = gate.begin('episode-2');
  assert.equal(first.isCurrent(), true);
  assert.equal(second.isCurrent(), true);
});

test('older batch result is ignored after a newer batch begins', () => {
  const gate = createLatestRequestGate();
  const older = gate.begin('__batch__');
  const newer = gate.begin('__batch__');
  let summary = null;
  older.commit(() => { summary = 'old'; });
  newer.commit(() => { summary = 'new'; });
  assert.equal(summary, 'new');
});
