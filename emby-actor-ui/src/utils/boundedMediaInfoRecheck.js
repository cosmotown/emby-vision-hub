export function collectUniqueReviewTargets(entries) {
  const targets = new Map();
  for (const entry of entries || []) {
    const targetId = entry?.target?.target_item_id;
    if (!targetId) continue;
    const key = String(targetId);
    if (!targets.has(key)) targets.set(key, []);
    const sourceId = String(entry.source_item_id);
    if (!targets.get(key).includes(sourceId)) targets.get(key).push(sourceId);
  }
  return [...targets.entries()].map(([targetId, sourceIds]) => ({ targetId, sourceIds }));
}

export async function runBoundedReadOnlyRecheck({
  entries,
  concurrency = 4,
  signal,
  isCurrent = () => true,
  requestRecheck,
  onResult = () => {},
  onProgress = () => {},
}) {
  const targets = collectUniqueReviewTargets(entries);
  const workerCount = Math.max(1, Math.min(Number(concurrency) || 1, 8, targets.length || 1));
  let cursor = 0;
  const summary = { total: targets.length, completed: 0, succeeded: 0, failed: 0, cancelled: false };

  const shouldStop = () => signal?.aborted || !isCurrent();
  const worker = async () => {
    while (!shouldStop()) {
      const index = cursor++;
      if (index >= targets.length) return;
      const target = targets[index];
      try {
        const result = await requestRecheck(target.targetId, signal);
        if (shouldStop()) return;
        onResult(target, result);
        summary.succeeded += 1;
      } catch (error) {
        if (shouldStop() || error?.name === 'AbortError' || error?.code === 'ERR_CANCELED') return;
        summary.failed += 1;
      }
      summary.completed += 1;
      onProgress({ ...summary });
    }
  };

  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  summary.cancelled = shouldStop();
  return summary;
}
