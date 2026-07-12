<template>
  <n-card :bordered="false" class="dashboard-card">
    <template #header>
      <span class="card-title">Webhook 队列诊断</span>
    </template>
    <template #header-extra>
      <n-space>
        <n-button size="small" ghost :loading="loading" @click="loadEvents()">刷新</n-button>
        <n-button size="small" type="primary" :loading="testing" @click="runDiagnostic">
          只读自检
        </n-button>
      </n-space>
    </template>

    <n-alert v-if="loadError" type="error" :show-icon="true" style="margin-bottom: 16px;">
      {{ loadError }}
    </n-alert>
    <n-alert
      v-else-if="summary.failed_count"
      type="warning"
      :show-icon="true"
      style="margin-bottom: 16px;"
    >
      当前有 {{ summary.failed_count }} 个失败事件，可在下方查看原因并重试。
    </n-alert>

    <div class="queue-summary">
      <div v-for="item in summaryItems" :key="item.key" class="summary-item">
        <span class="summary-label">{{ item.label }}</span>
        <strong :class="['summary-value', item.className]">{{ item.value }}</strong>
      </div>
    </div>

    <n-divider style="margin: 18px 0 12px;">最近事件</n-divider>

    <n-empty v-if="!loading && events.length === 0" description="暂无队列事件" />
    <div v-else class="event-list">
      <div v-for="event in events" :key="event.id" class="event-row">
        <div class="event-main">
          <div class="event-title-row">
            <strong>{{ event.task_name || event.task_kind }}</strong>
            <n-tag :type="statusMeta(event.status).type" size="small" :bordered="false">
              {{ statusMeta(event.status).label }}
            </n-tag>
            <n-tag v-if="event.event_source === 'diagnostic'" size="small" :bordered="false">
              只读测试
            </n-tag>
          </div>
          <div class="event-meta">
            <span>#{{ event.id }}</span>
            <span>{{ event.item_name || event.item_id || '无媒体信息' }}</span>
            <span>尝试 {{ event.attempt_count }}/{{ event.max_attempts }}</span>
            <span>{{ formatDate(event.updated_at || event.created_at) }}</span>
          </div>
          <div v-if="event.last_error" class="event-error">{{ event.last_error }}</div>
        </div>
        <n-button
          v-if="event.status === 'failed'"
          size="small"
          type="warning"
          ghost
          :loading="retryingId === event.id"
          @click="retryEvent(event.id)"
        >
          重试
        </n-button>
      </div>
    </div>
  </n-card>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import {
  NAlert,
  NButton,
  NCard,
  NDivider,
  NEmpty,
  NSpace,
  NTag,
  useMessage,
} from 'naive-ui';
import axios from 'axios';

const message = useMessage();
const events = ref([]);
const summary = ref({
  pending_count: 0,
  processing_count: 0,
  retry_count: 0,
  failed_count: 0,
  completed_count: 0,
});
const loading = ref(false);
const testing = ref(false);
const retryingId = ref(null);
const loadError = ref('');

const summaryItems = computed(() => [
  { key: 'pending', label: '待处理', value: summary.value.pending_count || 0, className: '' },
  { key: 'processing', label: '处理中', value: summary.value.processing_count || 0, className: 'is-running' },
  { key: 'retry', label: '待重试', value: summary.value.retry_count || 0, className: 'is-warning' },
  { key: 'failed', label: '失败', value: summary.value.failed_count || 0, className: 'is-error' },
  { key: 'completed', label: '已完成', value: summary.value.completed_count || 0, className: 'is-success' },
]);

const statusMeta = (status) => ({
  pending: { label: '待处理', type: 'default' },
  processing: { label: '处理中', type: 'info' },
  retry: { label: '等待重试', type: 'warning' },
  failed: { label: '失败', type: 'error' },
  completed: { label: '已完成', type: 'success' },
  superseded: { label: '已合并', type: 'default' },
}[status] || { label: status || '未知', type: 'default' });

const formatDate = (value) => {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
};

const loadEvents = async (silent = false, limit = 30) => {
  if (!silent) loading.value = true;
  try {
    const response = await axios.get(`/api/tasks/webhook-events?limit=${limit}`);
    events.value = response.data.events || [];
    summary.value = { ...summary.value, ...(response.data.summary || {}) };
    loadError.value = '';
    return events.value;
  } catch (error) {
    loadError.value = error.response?.data?.error || '无法读取 Webhook 队列';
    if (!silent) message.error(loadError.value);
    return [];
  } finally {
    if (!silent) loading.value = false;
  }
};

const waitForEvent = async (eventId) => {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    const latestEvents = await loadEvents(true, 200);
    const event = latestEvents.find((item) => item.id === eventId);
    if (event?.status === 'completed') return event;
    if (event?.status === 'failed') throw new Error(event.last_error || '队列事件执行失败');
  }
  return null;
};

const runDiagnostic = async () => {
  testing.value = true;
  try {
    const response = await axios.post('/api/tasks/webhook-events/diagnostic');
    const event = await waitForEvent(response.data.event_id);
    if (event) {
      message.success('队列自检通过：入队、调度和完成链路正常');
    } else {
      message.info('诊断事件仍在队列中，可能有其他后台任务正在执行');
    }
  } catch (error) {
    message.error(error.response?.data?.error || error.message || '队列自检失败');
  } finally {
    testing.value = false;
    await loadEvents(true);
  }
};

const retryEvent = async (eventId) => {
  retryingId.value = eventId;
  try {
    await axios.post(`/api/tasks/webhook-events/${eventId}/retry`);
    message.success(`事件 #${eventId} 已重新加入队列`);
    await loadEvents(true);
  } catch (error) {
    message.error(error.response?.data?.error || '重试失败');
  } finally {
    retryingId.value = null;
  }
};

onMounted(loadEvents);
</script>

<style scoped>
.queue-summary {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
}
.summary-item {
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--n-border-color);
  border-radius: 6px;
}
.summary-label {
  display: block;
  color: var(--n-text-color-3);
  font-size: 12px;
}
.summary-value {
  display: block;
  margin-top: 4px;
  font-size: 22px;
}
.summary-value.is-running { color: #2080f0; }
.summary-value.is-warning { color: #f0a020; }
.summary-value.is-error { color: #d03050; }
.summary-value.is-success { color: #18a058; }
.event-list {
  max-height: 420px;
  overflow-y: auto;
}
.event-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 2px;
  border-bottom: 1px solid var(--n-border-color);
}
.event-row:last-child { border-bottom: 0; }
.event-main { min-width: 0; }
.event-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.event-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-top: 5px;
  color: var(--n-text-color-3);
  font-size: 12px;
}
.event-error {
  margin-top: 7px;
  color: #d03050;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (max-width: 700px) {
  .queue-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .event-row { align-items: flex-start; }
}
</style>
