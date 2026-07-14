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

    <section class="protected-libraries-panel">
      <n-space justify="space-between" align="center" style="margin-bottom: 12px;">
        <div>
          <n-text strong>受保护（跳过清理）的媒体库</n-text>
          <n-text depth="3" style="display:block;font-size:13px;margin-top:4px;">
            保存后执行一次只读扫描。库中出现过的人物会持续受保护，即使没有 TMDb ID 或以后失去作品关联，也不会进入清理候选。
          </n-text>
        </div>
        <n-space :wrap="false">
          <n-button size="small" :loading="protectedLoading" @click="fetchProtectedLibraries">刷新媒体库</n-button>
          <n-button
            size="small"
            type="primary"
            :loading="protectedSaving"
            :disabled="isBackgroundBusy"
            @click="saveProtectedLibraries"
          >
            保存保护设置
          </n-button>
        </n-space>
      </n-space>
      <n-spin :show="protectedLoading">
        <n-checkbox-group v-model:value="selectedProtectedIds">
          <n-space v-if="protectedLibraries.length" wrap>
            <n-checkbox
              v-for="library in protectedLibraries"
              :key="library.library_id"
              :value="library.library_id"
            >
              <n-space align="center" :size="6">
                <span>{{ library.library_name }}</span>
                <n-tag v-if="library.missing" size="small" type="warning" :bordered="false">
                  Emby 中已不存在
                </n-tag>
                <n-tag v-if="library.protected_person_count" size="small" :bordered="false">
                  已保护 {{ library.protected_person_count }} 人
                </n-tag>
              </n-space>
            </n-checkbox>
          </n-space>
          <n-empty v-else-if="!protectedLoading" description="没有读取到可保护的 Emby 媒体库" size="small" />
        </n-checkbox-group>
      </n-spin>
    </section>

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

    <n-modal v-model:show="verifyModalVisible" :mask-closable="!verifyLoading">
      <n-card
        style="width: min(760px, 92vw); max-height: 82vh; overflow: auto;"
        :title="`核对详情：${verifyingCandidate?.person_name || '人物'}`"
        closable
        @close="verifyModalVisible = false"
      >
        <div v-if="verifyLoading" class="center-state"><n-spin size="large" /></div>
        <n-alert v-else-if="verifyError" type="error" title="核对失败">
          {{ verifyError }}
        </n-alert>
        <template v-else-if="verificationResult">
          <n-alert
            :type="verificationResult.status === 'orphan' ? 'success' : 'warning'"
            :title="verificationResult.status === 'orphan' ? '当前关联作品为 0' : `发现 ${verificationResult.reference_count} 部关联作品`"
            style="margin-bottom: 16px;"
          >
            {{ verificationResult.message }}
          </n-alert>

          <n-descriptions bordered :column="1" label-placement="left" style="margin-bottom: 16px;">
            <n-descriptions-item label="Emby ID">{{ verificationResult.person_id }}</n-descriptions-item>
            <n-descriptions-item label="外部 ID">{{ providerIdText(verificationResult.provider_ids) }}</n-descriptions-item>
            <n-descriptions-item label="核对结果">
              {{ verificationResult.reference_count }} 部当前关联作品
            </n-descriptions-item>
          </n-descriptions>

          <n-space style="margin-bottom: 16px;">
            <n-button
              v-if="personEmbyUrl"
              tag="a"
              :href="personEmbyUrl"
              target="_blank"
              secondary
            >
              在 Emby 查看人物
            </n-button>
            <n-button
              v-for="link in externalProfileLinks"
              :key="link.url"
              tag="a"
              :href="link.url"
              target="_blank"
              secondary
            >
              {{ link.label }}
            </n-button>
          </n-space>

          <div v-if="verificationResult.items?.length">
            <n-text strong>当前关联作品</n-text>
            <n-list bordered style="margin-top: 8px;">
              <n-list-item v-for="item in verificationResult.items" :key="item.id">
                <n-space justify="space-between" align="center" :wrap="false">
                  <div>
                    <strong>{{ item.series_name || item.name }}</strong>
                    <n-text depth="3" style="display:block;font-size:12px;">
                      {{ itemTypeLabel(item.type) }}{{ item.production_year ? ` · ${item.production_year}` : '' }}
                      <template v-if="item.series_name && item.name !== item.series_name"> · {{ item.name }}</template>
                    </n-text>
                  </div>
                  <n-button
                    v-if="embyItemUrl(item.id)"
                    tag="a"
                    :href="embyItemUrl(item.id)"
                    target="_blank"
                    size="small"
                    tertiary
                  >
                    在 Emby 打开
                  </n-button>
                </n-space>
              </n-list-item>
            </n-list>
            <n-text
              v-if="verificationResult.reference_count > verificationResult.items.length"
              depth="3"
              style="display:block;margin-top:8px;"
            >
              当前仅展示前 {{ verificationResult.items.length }} 部，共 {{ verificationResult.reference_count }} 部。
            </n-text>
          </div>

          <div v-if="verificationResult.status === 'orphan'">
            <n-divider>TMDb / IMDb 同身份对照</n-divider>
            <n-alert
              v-if="verificationResult.identity_comparison === 'unavailable'"
              type="warning"
              title="缺少外部身份"
            >
              该候选没有 TMDb 或 IMDb ID，无法查找 Emby 中的同身份人物，请结合姓名和头像人工判断。
            </n-alert>
            <n-alert
              v-else-if="verificationResult.identity_comparison === 'no_match'"
              type="info"
              title="没有同身份人物"
            >
              已按 TMDb/IMDb 精确查询，Emby 中没有找到其他同身份 Person 记录。
            </n-alert>
            <template v-else>
              <n-alert type="info" title="发现同身份人物" style="margin-bottom: 12px;">
                以下人物与当前候选拥有相同 TMDb/IMDb 身份，仅作为人工判断依据；不会自动删除或撤销当前候选。
              </n-alert>
              <div
                v-for="match in verificationResult.identity_matches"
                :key="match.person_id"
                class="identity-match"
              >
                <n-space justify="space-between" align="center">
                  <div>
                    <strong>{{ match.person_name }}</strong>
                    <n-text depth="3" style="display:block;font-size:12px;">
                      Emby: {{ match.person_id }} · 当前关联 {{ match.reference_count }} 部
                    </n-text>
                  </div>
                  <n-button
                    v-if="embyItemUrl(match.person_id)"
                    tag="a"
                    :href="embyItemUrl(match.person_id)"
                    target="_blank"
                    size="small"
                    secondary
                  >
                    查看人物
                  </n-button>
                </n-space>
                <n-list v-if="match.items?.length" bordered style="margin-top: 10px;">
                  <n-list-item v-for="item in match.items" :key="`${match.person_id}-${item.id}`">
                    <n-space justify="space-between" align="center" :wrap="false">
                      <div>
                        <strong>{{ item.series_name || item.name }}</strong>
                        <n-text depth="3" style="display:block;font-size:12px;">
                          {{ itemTypeLabel(item.type) }}{{ item.production_year ? ` · ${item.production_year}` : '' }}
                          <template v-if="item.series_name && item.name !== item.series_name"> · {{ item.name }}</template>
                        </n-text>
                      </div>
                      <n-button
                        v-if="embyItemUrl(item.id)"
                        tag="a"
                        :href="embyItemUrl(item.id)"
                        target="_blank"
                        size="small"
                        tertiary
                      >
                        打开作品
                      </n-button>
                    </n-space>
                  </n-list-item>
                </n-list>
                <n-text
                  v-if="match.reference_count > match.items.length"
                  depth="3"
                  style="display:block;margin-top:8px;"
                >
                  当前仅展示前 {{ match.items.length }} 部，共 {{ match.reference_count }} 部。
                </n-text>
              </div>
            </template>
          </div>
        </template>
      </n-card>
    </n-modal>
  </n-layout>
</template>

<script setup>
import { computed, h, onMounted, ref, watch } from 'vue';
import axios from 'axios';
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NCheckboxGroup,
  NDataTable,
  NDescriptions,
  NDescriptionsItem,
  NDivider,
  NEmpty,
  NImage,
  NLayout,
  NList,
  NListItem,
  NModal,
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
const verifyModalVisible = ref(false);
const verifyLoading = ref(false);
const verifyError = ref('');
const verifyingCandidate = ref(null);
const verificationResult = ref(null);
const protectedLibraries = ref([]);
const selectedProtectedIds = ref([]);
const protectedLoading = ref(false);
const protectedSaving = ref(false);
const pagination = { pageSize: 30, showSizePicker: true, pageSizes: [20, 30, 50, 100] };

const currentAction = computed(() => props.taskStatus?.current_action || '');
const isBackgroundBusy = computed(() => Boolean(props.taskStatus?.is_running));
const isScanRunning = computed(() => isBackgroundBusy.value && currentAction.value.includes('扫描幽灵人物'));
const isDeleteRunning = computed(() => isBackgroundBusy.value && currentAction.value.includes('删除') && currentAction.value.includes('幽灵人物'));

const imageUrl = (personId) => `/image_proxy/Items/${personId}/Images/Primary?maxWidth=160&quality=85`;
const isVerifiedOrphan = (row) => Boolean(row.last_checked_at && !row.last_error);
const formatDate = (value) => {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
};

const columns = [
  { type: 'selection', multiple: true, disabled: (row) => !isVerifiedOrphan(row) },
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
    render: (row) => {
      if (row.last_error) {
        return h(NText, { type: 'error', style: 'white-space:normal;overflow-wrap:anywhere;' }, () => row.last_error);
      }
      if (isVerifiedOrphan(row)) {
        return h('div', null, [
          h(NTag, { type: 'success', bordered: false }, () => '已核对：0 部作品'),
          h(NText, { depth: 3, style: 'display:block;font-size:12px;margin-top:3px;' }, () => formatDate(row.last_checked_at)),
        ]);
      }
      return h(NTag, { type: 'default', bordered: false }, () => '需要实时核对');
    },
  },
  {
    title: '操作',
    key: 'actions',
    width: 120,
    fixed: 'right',
    render: (row) => h(NButton, {
      size: 'small',
      type: isVerifiedOrphan(row) ? 'default' : 'primary',
      secondary: isVerifiedOrphan(row),
      onClick: () => verifyCandidate(row),
    }, () => isVerifiedOrphan(row) ? '重新核对' : '核对详情'),
  },
];

const providerIdText = (providerIds) => {
  let normalized = providerIds || {};
  if (typeof normalized === 'string') {
    try { normalized = JSON.parse(normalized); } catch { normalized = {}; }
  }
  const labels = Object.entries(normalized).map(([key, value]) => `${key}: ${value}`);
  return labels.length ? labels.join(' / ') : '无';
};

const embyUrlForItem = (itemId) => {
  const baseUrl = verificationResult.value?.emby_url?.replace(/\/$/, '');
  if (!baseUrl || !itemId) return '';
  const serverId = verificationResult.value?.emby_server_id;
  return `${baseUrl}/web/index.html#!/item?id=${encodeURIComponent(itemId)}${serverId ? `&serverId=${encodeURIComponent(serverId)}` : ''}`;
};

const personEmbyUrl = computed(() => embyUrlForItem(verificationResult.value?.person_id));
const embyItemUrl = (itemId) => embyUrlForItem(itemId);
const externalProfileLinks = computed(() => {
  let providerIds = verificationResult.value?.provider_ids || {};
  if (typeof providerIds === 'string') {
    try { providerIds = JSON.parse(providerIds); } catch { providerIds = {}; }
  }
  const links = [];
  for (const [key, value] of Object.entries(providerIds)) {
    if (!value) continue;
    const provider = key.toLowerCase();
    if (provider === 'tmdb') {
      links.push({ label: '查看 TMDb', url: `https://www.themoviedb.org/person/${encodeURIComponent(value)}` });
    } else if (provider === 'imdb') {
      links.push({ label: '查看 IMDb', url: `https://www.imdb.com/name/${encodeURIComponent(value)}/` });
    }
  }
  return links;
});

const itemTypeLabel = (type) => ({
  Movie: '电影',
  Series: '剧集',
  Episode: '分集',
  Video: '视频',
  MusicVideo: '音乐视频',
}[type] || type || '媒体');

const verifyCandidate = async (row) => {
  verifyingCandidate.value = row;
  verificationResult.value = null;
  verifyError.value = '';
  verifyLoading.value = true;
  verifyModalVisible.value = true;
  try {
    const response = await axios.post(`/api/person-cleanup/candidates/${encodeURIComponent(row.person_id)}/verify`);
    verificationResult.value = response.data;
    if (response.data.candidate_removed) {
      candidates.value = candidates.value.filter((item) => item.person_id !== row.person_id);
      selectedIds.value = selectedIds.value.filter((personId) => personId !== row.person_id);
      message.warning(response.data.message || '发现关联作品，已撤销候选');
    } else if (response.data.candidate) {
      const index = candidates.value.findIndex((item) => item.person_id === row.person_id);
      if (index >= 0) candidates.value[index] = response.data.candidate;
      message.success(response.data.message || '核对完成，可以人工勾选');
    }
  } catch (error) {
    verifyError.value = error.response?.data?.error || '无法完成人物关联核对';
  } finally {
    verifyLoading.value = false;
  }
};

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

const fetchProtectedLibraries = async () => {
  protectedLoading.value = true;
  try {
    const response = await axios.get('/api/person-cleanup/protected-libraries');
    protectedLibraries.value = response.data.libraries || [];
    selectedProtectedIds.value = protectedLibraries.value
      .filter((library) => library.selected)
      .map((library) => library.library_id);
  } catch (error) {
    message.error(error.response?.data?.error || '无法读取受保护媒体库');
  } finally {
    protectedLoading.value = false;
  }
};

const saveProtectedLibraries = async () => {
  protectedSaving.value = true;
  try {
    const response = await axios.post('/api/person-cleanup/protected-libraries', {
      library_ids: selectedProtectedIds.value,
    });
    message.success(response.data.message || '保护设置已保存');
    await fetchProtectedLibraries();
  } catch (error) {
    message.error(error.response?.data?.error || '无法保存受保护媒体库');
  } finally {
    protectedSaving.value = false;
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
      fetchProtectedLibraries();
    }
  },
);

onMounted(() => {
  fetchCandidates();
  fetchProtectedLibraries();
});
</script>

<style scoped>
.center-state {
  display: flex;
  min-height: 240px;
  align-items: center;
  justify-content: center;
}

.protected-libraries-panel {
  padding: 16px;
  margin-bottom: 16px;
  border: 1px solid var(--n-border-color);
  border-radius: 6px;
}

.identity-match {
  padding: 12px 0;
  border-bottom: 1px solid var(--n-border-color);
}

.identity-match:last-child {
  border-bottom: 0;
}
</style>
