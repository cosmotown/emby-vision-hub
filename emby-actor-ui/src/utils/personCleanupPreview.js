export const PERSON_CLEANUP_PREVIEW_LABELS = Object.freeze({
  verified_orphan: '显式孤儿人物',
  identity_alias_only: '同身份别名',
  people_unavailable: 'People 无法完整核验',
  invalid_response: 'Emby 响应异常',
  connection_failed: '连接失败',
  linked: '重新发现关联',
  protected_library_alias: '保护库 alias',
  protected_library_unverifiable: '保护库不可核验',
  protected_id: '保护人物 ID',
  protected_name: '保护人物姓名',
  protected_provider_identity: '保护人物外部身份',
});

export const personCleanupPreviewLabel = (status) => (
  PERSON_CLEANUP_PREVIEW_LABELS[status] || `其他：${status || 'unknown'}`
);

export const personCleanupPreviewPercentage = (count, total) => {
  const normalizedCount = Number(count) || 0;
  const normalizedTotal = Number(total) || 0;
  if (normalizedTotal <= 0) return '0.00%';
  return `${((normalizedCount * 100) / normalizedTotal).toFixed(2)}%`;
};

export const buildPersonCleanupPreviewRows = (summary) => {
  const total = Number(summary?.candidate_total) || 0;
  return (summary?.states || []).map((entry) => ({
    ...entry,
    label: personCleanupPreviewLabel(entry.status),
    percentage_text: personCleanupPreviewPercentage(entry.count, total),
    sample_available: entry.status !== 'verified_orphan' && Number(entry.count) > 0,
  }));
};
