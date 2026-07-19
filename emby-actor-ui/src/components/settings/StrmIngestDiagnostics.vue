<template>
  <n-card :bordered="false" class="dashboard-card">
    <template #header><span class="card-title">STRM 入库诊断</span></template>
    <template #header-extra>
      <n-button size="small" ghost :loading="loading" @click="loadEvents">刷新</n-button>
    </template>

    <n-alert v-if="loadError" type="error" :show-icon="true" style="margin-bottom: 14px;">
      {{ loadError }}
    </n-alert>
    <n-alert v-else-if="summary.failed_count" type="warning" :show-icon="true" style="margin-bottom: 14px;">
      {{ summary.failed_count }} 个 STRM 已达到重试上限并停止自动刷新，请人工核对。
    </n-alert>

    <div class="summary-grid">
      <div v-for="item in summaryItems" :key="item.key" class="summary-item">
        <span>{{ item.label }}</span>
        <strong :class="item.className">{{ item.value }}</strong>
      </div>
    </div>

    <n-divider style="margin: 18px 0 12px;">待处理与异常路径</n-divider>
    <n-empty v-if="!loading && events.length === 0" description="当前没有 STRM 入库异常" />
    <div v-else class="event-list">
      <div v-for="event in events" :key="event.id" class="event-row">
        <div class="event-main">
          <div class="title-row">
            <n-tag :type="statusMeta(event.status).type" size="small" :bordered="false">
              {{ statusMeta(event.status).label }}
            </n-tag>
            <strong class="path" :title="event.file_path">{{ event.file_path }}</strong>
          </div>
          <div class="meta-row">
            <span>尝试 {{ event.attempt_count }}/{{ event.max_attempts }}</span>
            <span>{{ event.operation === 'delete' ? '删除同步' : '入库同步' }}</span>
            <span>来源 {{ event.source }}</span>
            <span v-if="event.next_attempt_at">下次 {{ formatDate(event.next_attempt_at) }}</span>
          </div>
          <div v-if="event.last_error" class="error-text">{{ event.last_error }}</div>
        </div>
        <n-space :wrap="false">
          <n-button size="small" quaternary @click="copyPath(event.file_path)">复制路径</n-button>
          <n-button
            v-if="['failed', 'ignored', 'cancelled'].includes(event.status)"
            size="small"
            type="warning"
            ghost
            :loading="workingId === event.id"
            @click="retryEvent(event.id)"
          >重试</n-button>
          <n-button
            v-if="['pending', 'retry', 'failed', 'cancelled'].includes(event.status)"
            size="small"
            ghost
            :loading="workingId === event.id"
            @click="ignoreEvent(event.id)"
          >忽略</n-button>
        </n-space>
      </div>
    </div>
  </n-card>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import axios from 'axios';
import { NAlert, NButton, NCard, NDivider, NEmpty, NSpace, NTag, useMessage } from 'naive-ui';

const message = useMessage();
const loading = ref(false);
const workingId = ref(null);
const loadError = ref('');
const events = ref([]);
const summary = ref({
  pending_count: 0,
  processing_count: 0,
  retry_count: 0,
  failed_count: 0,
  completed_count: 0,
  ignored_count: 0,
});

const summaryItems = computed(() => [
  { key: 'pending', label: '待处理', value: summary.value.pending_count || 0, className: '' },
  { key: 'processing', label: '处理中', value: summary.value.processing_count || 0, className: 'running' },
  { key: 'retry', label: '等待重试', value: summary.value.retry_count || 0, className: 'warning' },
  { key: 'failed', label: '人工处理', value: summary.value.failed_count || 0, className: 'error' },
  { key: 'ignored', label: '已忽略', value: summary.value.ignored_count || 0, className: '' },
]);

const statusMeta = (status) => ({
  pending: { label: '等待首次重试', type: 'default' },
  processing: { label: '正在核对', type: 'info' },
  retry: { label: '等待重试', type: 'warning' },
  failed: { label: '需要人工处理', type: 'error' },
  ignored: { label: '已忽略', type: 'default' },
  cancelled: { label: '文件已消失', type: 'default' },
}[status] || { label: status || '未知', type: 'default' });

const formatDate = (value) => {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
};

const loadEvents = async () => {
  loading.value = true;
  try {
    const response = await axios.get('/api/tasks/strm-ingest-events?limit=100');
    events.value = response.data.events || [];
    summary.value = { ...summary.value, ...(response.data.summary || {}) };
    loadError.value = '';
  } catch (error) {
    loadError.value = error.response?.data?.error || '无法读取 STRM 入库诊断';
  } finally {
    loading.value = false;
  }
};

const retryEvent = async (eventId) => {
  workingId.value = eventId;
  try {
    await axios.post(`/api/tasks/strm-ingest-events/${eventId}/retry`);
    message.success('已重新加入 STRM 入库队列');
    await loadEvents();
  } catch (error) {
    message.error(error.response?.data?.error || '重试失败');
  } finally {
    workingId.value = null;
  }
};

const ignoreEvent = async (eventId) => {
  workingId.value = eventId;
  try {
    await axios.post(`/api/tasks/strm-ingest-events/${eventId}/ignore`);
    message.success('已忽略该 STRM 入库异常');
    await loadEvents();
  } catch (error) {
    message.error(error.response?.data?.error || '忽略失败');
  } finally {
    workingId.value = null;
  }
};

const copyPath = async (path) => {
  try {
    await navigator.clipboard.writeText(path);
    message.success('路径已复制');
  } catch {
    message.warning('浏览器无法访问剪贴板，请手动复制');
  }
};

onMounted(loadEvents);
</script>

<style scoped>
.summary-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
}
.summary-item {
  padding: 10px 12px;
  border: 1px solid var(--n-border-color);
  border-radius: 6px;
}
.summary-item span { display: block; color: var(--n-text-color-3); font-size: 12px; }
.summary-item strong { display: block; margin-top: 4px; font-size: 22px; }
.summary-item strong.running { color: #2080f0; }
.summary-item strong.warning { color: #f0a020; }
.summary-item strong.error { color: #d03050; }
.event-list { max-height: 430px; overflow-y: auto; }
.event-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 12px 2px;
  border-bottom: 1px solid var(--n-border-color);
}
.event-main { min-width: 0; flex: 1; }
.title-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
.path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meta-row { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 5px; color: var(--n-text-color-3); font-size: 12px; }
.error-text { margin-top: 5px; color: var(--n-error-color); font-size: 12px; word-break: break-word; }
@media (max-width: 700px) {
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .event-row { align-items: flex-start; flex-direction: column; }
}
</style>
