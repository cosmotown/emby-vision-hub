<template>
  <n-layout content-style="padding: 24px;">
    <n-page-header>
      <template #title>
        <n-space align="center">
          <span>人物清理</span>
          <n-tag type="warning" round :bordered="false" size="small">
            {{ candidates.length }} 位待复核
          </n-tag>
        </n-space>
      </template>
      <template #extra>
        <n-space>
          <n-button :loading="loading" @click="fetchCandidates">刷新</n-button>
          <n-button
            type="primary"
            :loading="isScanRunning"
            :disabled="isBackgroundBusy && !isScanRunning"
            @click="scanCandidates"
          >
            只读扫描
          </n-button>
          <n-button
            type="error"
            :loading="isDeleteRunning"
            :disabled="selectedIds.length === 0 || isBackgroundBusy"
            @click="confirmDelete"
          >
            删除选中 ({{ selectedIds.length }})
          </n-button>
        </n-space>
      </template>
    </n-page-header>

    <n-alert type="warning" title="安全说明" style="margin: 20px 0;">
      扫描只生成候选，不会删除人物。删除仅处理人工勾选项，并会在每次删除前重新查询 Emby；发现任何关联作品或复核失败都会跳过。删除接口需要神医 Pro 支持。
    </n-alert>

    <n-alert
      v-if="taskStatus?.last_action?.includes('幽灵人物') && taskStatus?.message"
      :type="taskStatus.progress < 0 || taskStatus.message.includes('失败') ? 'error' : 'info'"
      style="margin-bottom: 16px;"
    >
      {{ taskStatus.message }}
    </n-alert>

    <div v-if="loading" class="center-state"><n-spin size="large" /></div>
    <n-alert v-else-if="loadError" type="error" title="加载失败">{{ loadError }}</n-alert>
    <n-empty v-else-if="candidates.length === 0" description="暂无候选，请先执行只读扫描" size="large" />
    <n-data-table
      v-else
      v-model:checked-row-keys="selectedIds"
      :columns="columns"
      :data="candidates"
      :row-key="row => row.person_id"
      :pagination="pagination"
      :scroll-x="900"
    />
  </n-layout>
</template>

<script setup>
import { computed, h, onMounted, ref, watch } from 'vue';
import axios from 'axios';
import {
  NAlert,
  NButton,
  NDataTable,
  NEmpty,
  NImage,
  NLayout,
  NPageHeader,
  NSpace,
  NSpin,
  NTag,
  NText,
  useDialog,
  useMessage,
} from 'naive-ui';

const props = defineProps({
  taskStatus: { type: Object, required: true },
});

const message = useMessage();
const dialog = useDialog();
const candidates = ref([]);
const selectedIds = ref([]);
const loading = ref(false);
const loadError = ref('');
const pagination = { pageSize: 30, showSizePicker: true, pageSizes: [20, 30, 50, 100] };

const currentAction = computed(() => props.taskStatus?.current_action || '');
const isBackgroundBusy = computed(() => Boolean(props.taskStatus?.is_running));
const isScanRunning = computed(() => isBackgroundBusy.value && currentAction.value.includes('扫描幽灵人物'));
const isDeleteRunning = computed(() => isBackgroundBusy.value && currentAction.value.includes('删除') && currentAction.value.includes('幽灵人物'));

const imageUrl = (personId) => `/image_proxy/Items/${personId}/Images/Primary?maxWidth=160&quality=85`;
const formatDate = (value) => {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
};

const columns = [
  { type: 'selection', multiple: true },
  {
    title: '头像',
    key: 'avatar',
    width: 76,
    render: (row) => h(NImage, {
      src: imageUrl(row.person_id),
      width: 48,
      height: 48,
      objectFit: 'cover',
      previewDisabled: true,
      fallbackSrc: '/default-avatar.png',
      style: 'border-radius: 4px;',
    }),
  },
  {
    title: '人物',
    key: 'person_name',
    minWidth: 180,
    render: (row) => h('div', null, [
      h('strong', null, row.person_name || '未知人物'),
      h(NText, { depth: 3, style: 'display:block;font-size:12px;margin-top:3px;' }, () => `Emby: ${row.person_id}`),
    ]),
  },
  {
    title: '外部 ID',
    key: 'provider_ids_json',
    minWidth: 150,
    render: (row) => {
      let providerIds = row.provider_ids_json || {};
      if (typeof providerIds === 'string') {
        try { providerIds = JSON.parse(providerIds); } catch { providerIds = {}; }
      }
      const labels = Object.entries(providerIds).map(([key, value]) => `${key}: ${value}`);
      return labels.length ? labels.join(' / ') : '无';
    },
  },
  {
    title: '扫描时间',
    key: 'discovered_at',
    width: 180,
    render: (row) => formatDate(row.discovered_at),
  },
  {
    title: '复核状态',
    key: 'last_error',
    minWidth: 220,
    render: (row) => row.last_error
      ? h(NText, { type: 'error', style: 'white-space:normal;overflow-wrap:anywhere;' }, () => row.last_error)
      : h(NTag, { type: 'default', bordered: false }, () => '等待人工选择'),
  },
];

const fetchCandidates = async () => {
  loading.value = true;
  try {
    const response = await axios.get('/api/person-cleanup/candidates');
    candidates.value = response.data.candidates || [];
    const validIds = new Set(candidates.value.map((item) => item.person_id));
    selectedIds.value = selectedIds.value.filter((personId) => validIds.has(personId));
    loadError.value = '';
  } catch (error) {
    loadError.value = error.response?.data?.error || '无法读取人物候选';
  } finally {
    loading.value = false;
  }
};

const scanCandidates = async () => {
  try {
    const response = await axios.post('/api/person-cleanup/scan');
    selectedIds.value = [];
    message.success(response.data.message || '只读扫描已提交');
  } catch (error) {
    message.error(error.response?.data?.error || '扫描任务提交失败');
  }
};

const confirmDelete = () => {
  const selectedNames = candidates.value
    .filter((item) => selectedIds.value.includes(item.person_id))
    .slice(0, 8)
    .map((item) => item.person_name)
    .join('、');
  dialog.warning({
    title: '确认删除选中人物',
    content: `将复核并尝试删除 ${selectedIds.value.length} 位人物：${selectedNames}${selectedIds.value.length > 8 ? ' 等' : ''}。此操作会修改 Emby 人物库，删除前会逐一检查关联作品。`,
    positiveText: '确认复核并删除',
    negativeText: '取消',
    onPositiveClick: deleteSelected,
  });
};

const deleteSelected = async () => {
  try {
    const response = await axios.post('/api/person-cleanup/delete', {
      person_ids: selectedIds.value,
    });
    message.success(response.data.message || '删除任务已提交');
  } catch (error) {
    message.error(error.response?.data?.error || '删除任务提交失败');
  }
};

watch(
  () => props.taskStatus?.is_running,
  (isRunning, wasRunning) => {
    if (wasRunning && !isRunning && props.taskStatus?.last_action?.includes('幽灵人物')) {
      fetchCandidates();
    }
  },
);

onMounted(fetchCandidates);
</script>

<style scoped>
.center-state {
  display: flex;
  min-height: 240px;
  align-items: center;
  justify-content: center;
}
</style>
