export function createLatestRequestGate() {
  const generations = new Map();
  const controllers = new Map();
  let active = true;

  const invalidate = (key) => {
    const normalized = String(key);
    controllers.get(normalized)?.abort();
    controllers.delete(normalized);
    const generation = (generations.get(normalized) || 0) + 1;
    generations.set(normalized, generation);
    return generation;
  };

  const begin = (key) => {
    const normalized = String(key);
    const generation = invalidate(normalized);
    const controller = new AbortController();
    controllers.set(normalized, controller);
    const isCurrent = () => active && generations.get(normalized) === generation;
    return {
      generation,
      signal: controller.signal,
      isCurrent,
      commit(callback) {
        if (!isCurrent()) return false;
        callback();
        return true;
      },
      finish() {
        if (isCurrent()) controllers.delete(normalized);
      },
    };
  };

  const dispose = () => {
    active = false;
    for (const controller of controllers.values()) controller.abort();
    controllers.clear();
  };

  return { begin, invalidate, dispose, isActive: () => active };
}
