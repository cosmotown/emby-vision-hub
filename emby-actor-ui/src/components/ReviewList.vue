<template>
  <n-layout content-style="padding: 24px;">
  <n-card class="dashboard-card" :bordered="false" size="small">
    
    <n-alert title="手动处理操作提示" type="info" style="margin-top: 24px;">
          可在下方搜索框输入片名直接搜索处理，也可以搜索存量剧集点击 <n-icon :component="AddToWatchlistIcon" /> 加入智能追剧 <br />
          可点击待复核列表媒体项进入手动编辑页面、可一键清空待复核列表转到已处理记录。
    </n-alert>
    <!-- ✅ [修正] Access prop via `props.taskStatus` -->
    <n-alert 
      v-if="props.taskStatus?.is_running" 
      title="后台任务运行中" 
      type="warning" 
      style="margin-bottom: 20px;"
      closable
    >
      后台任务正在运行，此时“手动处理”等操作可能会失败。
    </n-alert>
    
    <div>
      <n-input
        v-model:value="searchQuery"
        placeholder="输入媒体名称搜索整个 Emby 库..."
        clearable
        @keyup.enter="handleSearch"
        @clear="handleSearch"
        style="margin-bottom: 20px; max-width: 400px;"
      >
        <template #suffix>
          <n-icon :component="SearchIcon" @click="handleSearch" style="cursor: pointer;" />
        </template>
      </n-input>
      <n-popconfirm
          @positive-click="clearAllReviewItems"
          :positive-button-props="{ type: 'error' }"
        >
          <template #trigger>
            <n-button type="error" ghost :disabled="tableData.length === 0 || loading || isShowingSearchResults">
              <template #icon><n-icon :component="TrashIcon" /></template>
              清空所有待复核项
            </n-button>
          </template>
          确定要清空所有 {{ totalItems }} 条待复核记录吗？此操作不可恢复。
        </n-popconfirm>
        <n-popconfirm
            @positive-click="reprocessAllReviewItems"
        >
            <template #trigger>
                <!-- ✅ [修正] Access prop via `props.taskStatus` -->
                <n-button type="warning" ghost :disabled="tableData.length === 0 || loading || props.taskStatus?.is_running || isShowingSearchResults">
                    <template #icon><n-icon :component="ReprocessIcon" /></template>
                    重新处理所有
                </n-button>
            </template>
            确定要重新处理所有 {{ totalItems }} 条待复核记录吗？
        </n-popconfirm>
        <n-button
          type="info"
          ghost
          :loading="globalRecheck.running"
          :disabled="isShowingSearchResults || totalItems === 0 || globalRecheck.running"
          @click="recheckAllMediaInfo"
        >
          重新核对全部
        </n-button>
        <n-button
          v-if="globalRecheck.running"
          type="error"
          ghost
          @click="cancelRecheckAllMediaInfo"
        >
          停止核对
        </n-button>
        <n-popconfirm
          @positive-click="repairSelectedMediaInfo"
          :positive-button-props="{ type: 'warning' }"
        >
          <template #trigger>
            <n-button
              type="warning"
              ghost
              :loading="batchRepairLoading"
              :disabled="checkedRowKeys.length === 0 || checkedRowKeys.length > 20 || !repairFeatureEnabled || batchRepairLoading"
            >
              调用神医修复已选项 ({{ checkedRowKeys.length }}/20)
            </n-button>
          </template>
          <div style="max-width: 420px;">
            将逐项调用神医单 Item MediaInfo 接口，可能访问 STRM 对应的远程媒体，
            并可能由神医触发 ffprobe/rffmpeg。不会刷新整季、整剧或媒体库，
            EVH 也不会自行执行 ffprobe。确认继续吗？
          </div>
        </n-popconfirm>

      <n-alert
        v-if="globalRecheck.running || globalRecheck.completed > 0"
        :type="globalRecheck.failed > 0 ? 'warning' : 'info'"
        style="margin: 16px 0;"
      >
        全部核对进度：{{ globalRecheck.completed }}/{{ globalRecheck.total }}，
        成功 {{ globalRecheck.succeeded }}，失败 {{ globalRecheck.failed }}
        <span v-if="globalRecheck.cancelled">（已停止）</span>
      </n-alert>

      <n-spin :show="loading">
        <div v-if="error" class="error-message">
          <n-alert title="加载错误" type="error">{{ error }}</n-alert>
        </div>
        <div v-else>
          <n-data-table
            v-if="tableData.length > 0"
            :columns="columns"
            :data="tableData"
            :pagination="paginationProps"
            :bordered="false"
            :single-line="false" 
            striped
            size="small"
            :row-key="row => row.item_id"
            :loading="loadingAction[currentRowId]"
            :checked-row-keys="checkedRowKeys"
            @update:checked-row-keys="keys => checkedRowKeys = keys"
            remote 
          />
          <n-empty 
            v-else-if="!loading && tableData.length === 0" 
            :description="isShowingSearchResults ? '在 Emby 库中未找到匹配项。' : '太棒了！没有需要手动处理的媒体项。'" 
            style="margin-top: 50px; margin-bottom: 30px;" 
          />
        </div>
      </n-spin>
    </div>
  </n-card>
  </n-layout>
</template>

<script setup>
import { useRouter } from 'vue-router';
import { ref, onMounted, onBeforeUnmount, computed, h } from 'vue';
import axios from 'axios';
import {
    NCard, NSpin, NAlert, NText, NDataTable, NButton, NSpace, NPopconfirm, NEmpty, NInput, NIcon,
    NTag, useMessage
} from 'naive-ui';
import { HeartOutline as AddToWatchlistIcon } from '@vicons/ionicons5';
import { SearchOutline as SearchIcon, PlayForwardOutline as ReprocessIcon, CheckmarkCircleOutline as MarkDoneIcon, TrashOutline as TrashIcon } from '@vicons/ionicons5';

import { useConfig } from '../composables/useConfig';
import { createLatestRequestGate } from '../utils/latestRequestGate';
import { collectUniqueReviewTargets, runBoundedReadOnlyRecheck } from '../utils/boundedMediaInfoRecheck';

// ✅ [修正] defineProps returns an object, which we've named `props`.
const props = defineProps({
  taskStatus: {
    type: Object,
    required: true,
    // Providing a default is still good practice, but required: true makes it mandatory.
    default: () => ({ is_running: false }) 
  }
});

const router = useRouter();
const message = useMessage();
const { configModel } = useConfig();

const tableData = ref([]);
const loading = ref(true);
const error = ref(null);
const totalItems = ref(0);
const currentPage = ref(1);
const itemsPerPage = ref(15);
const searchQuery = ref('');
const loadingAction = ref({});
const currentRowId = ref(null);
const isShowingSearchResults = ref(false);
const checkedRowKeys = ref([]);
const mediaInfoStatuses = ref({});
const batchRepairLoading = ref(false);
const globalRecheck = ref({
  running: false,
  total: 0,
  completed: 0,
  succeeded: 0,
  failed: 0,
  cancelled: false,
});
let globalRecheckGeneration = 0;
let globalRecheckController = null;
let mediaInfoPollTimer = null;
const mediaInfoRequestGate = createLatestRequestGate();
const listRequestGate = createLatestRequestGate();
const repairFeatureEnabled = computed(() => configModel.value?.shenyi_mediainfo_repair_enabled === true);

const rowTargetId = (row) => {
  const explicit = row?.media_info_target?.target_item_id;
  if (explicit) return String(explicit);
  if (isShowingSearchResults.value && ['Movie', 'Episode'].includes(row?.item_type)) {
    return String(row.item_id);
  }
  return null;
};

const mediaStatusLabels = {
  present: 'STRM存在',
  missing: 'STRM缺失',
  unreadable: 'STRM不可读',
  invalid_content: 'STRM无效',
  path_unmapped: '路径未映射',
  indexed: 'Emby已收录',
  not_indexed: 'Emby未收录',
  path_mismatch: '路径不匹配',
  duplicate_match: '重复匹配',
  lookup_failed: '查询失败',
  present_valid: '神医持久化有效',
  present_invalid: '神医持久化无效',
  present_unreadable: '持久化不可读',
  not_configured: '持久化未配置',
  not_observable: '持久化不可观察',
  identity_mismatch: '持久化身份不符',
  ready: '媒体流就绪',
  partial: '媒体流部分就绪',
  media_source_missing: 'MediaSource缺失',
  media_streams_empty: 'MediaStreams为空',
  video_stream_missing: '视频流缺失',
  read_failed: '媒体流读取失败',
  unknown: '未知',
};

const statusTagType = (status) => {
  if (['present', 'indexed', 'present_valid', 'ready'].includes(status)) return 'success';
  if (['unknown', 'not_configured', 'not_observable'].includes(status)) return 'default';
  if (['partial', 'media_streams_empty', 'video_stream_missing'].includes(status)) return 'warning';
  return 'error';
};

const statusTag = (label, layer) => h(
  NTag,
  { size: 'small', type: statusTagType(layer?.status), bordered: false },
  { default: () => `${label}: ${mediaStatusLabels[layer?.status] || layer?.status || '未知'}` }
);

// ... other functions like addToWatchlist, formatDate, etc. remain unchanged ...
const addToWatchlist = async (rowData) => {
  if (rowData.item_type !== 'Series') {
    message.warning('只有剧集类型才能添加到追剧列表。');
    return;
  }
  const tmdbId = rowData.provider_ids?.Tmdb;
  if (!tmdbId) {
      message.error('无法添加到追剧列表：此项目缺少TMDb ID。');
      return;
  }
  try {
    const payload = {
      item_id: rowData.item_id,
      tmdb_id: tmdbId,
      item_name: rowData.item_name,
      item_type: rowData.item_type,
    };
    const response = await axios.post('/api/watchlist/add', payload);
    message.success(response.data.message || '添加成功！');
  } catch (error) {
    message.error(error.response?.data?.error || '添加到追剧列表失败，可能已存在。');
  }
};

const formatDate = (timestamp) => {
  if (!timestamp) return 'N/A';
  try {
    const date = new Date(timestamp);
    if (isNaN(date.getTime())) return '无效日期';
    return date.toLocaleString('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false
    });
  } catch (e) {
    return '日期格式化错误';
  }
};

const clearAllReviewItems = async () => {
  loading.value = true;
  try {
    const response = await axios.post('/api/actions/clear_review_items');
    message.success(response.data.message);
    await fetchReviewItems(); 
  } catch (err) {
    console.error("清空待复核列表失败:", err);
    message.error(`操作失败: ${err.response?.data?.error || err.message}`);
  } finally {
    loading.value = false;
  }
};

const goToEditPage = (row) => {
  if (row && row.item_id) {
    router.push({ name: 'MediaEditPage', params: { itemId: row.item_id } });
  } else {
    message.error("无效的媒体项，无法跳转到编辑页面！");
  }
};

const handleMarkAsProcessed = async (row) => {
  currentRowId.value = row.item_id;
  loadingAction.value[row.item_id] = true;
  try {
    await axios.post(`/api/actions/mark_item_processed/${row.item_id}`);
    message.success(`项目 "${row.item_name}" 已标记为已处理。`);
    await fetchReviewItems();
  } catch (err) {
    console.error("标记为已处理失败:", err);
    message.error(`标记项目 "${row.item_name}" 为已处理失败: ${err.response?.data?.error || err.message}`);
  } finally {
    loadingAction.value[row.item_id] = false;
    currentRowId.value = null;
  }
};


const columns = computed(() => [
  {
    type: 'selection',
    disabled(row) {
      return !rowTargetId(row);
    }
  },
  {
    title: '媒体名称 (ID)',
    key: 'item_name',
    resizable: true,
    render(row) {
      return h('div', { 
        style: 'cursor: pointer; padding: 5px 0;',
        onClick: () => goToEditPage(row)
      }, [
        h(NText, { strong: true }, { default: () => row.item_name || '未知名称' }),
        h(NText, { depth: 3, style: 'font-size: 0.8em; display: block; margin-top: 2px;' }, { default: () => `(ID: ${row.item_id || 'N/A'})` })
        ,
        row.media_info_target?.target_resolution === 'series_episode'
          ? h(NText, { depth: 3, style: 'font-size: 0.8em; display: block;' }, {
              default: () => `MediaInfo 目标: S${row.media_info_target.target_parent_index_number}E${row.media_info_target.target_index_number} / Item ${row.media_info_target.target_item_id}`
            })
          : null
      ]);
    }
  },
  { 
    title: '类型', 
    key: 'item_type', 
    width: 80, 
    resizable: true,
    render(row) {
      const typeMap = { 'Movie': '电影', 'Series': '电视剧', 'Episode': '剧集' };
      return typeMap[row.item_type] || row.item_type;
    }
  },
  {
    title: '记录时间',
    key: 'failed_at',
    width: 170,
    resizable: true,
    render(row) { return isShowingSearchResults.value ? 'N/A' : formatDate(row.failed_at); }
  },
  // ✅【关键修复】将 key 从 'error_message' 改为 'reason'
  { title: '原因', key: 'reason', resizable: true, ellipsis: { tooltip: true } },
  {
    title: '评分',
    key: 'score',
    width: 80,
    resizable: true,
    render(row) {
      return row.score !== null && row.score !== undefined ? row.score.toFixed(1) : 'N/A';
    }
  },
  {
    title: 'MediaInfo 四层状态',
    key: 'media_info_status',
    width: 260,
    render(row) {
      const status = mediaInfoStatuses.value[row.item_id];
      if (!status) {
        const reason = row.media_info_target?.target_reason_code;
        return h(NText, { depth: 3 }, { default: () => reason ? `目标不可确定: ${reason}` : '尚未读取' });
      }
      return h(NSpace, { vertical: true, size: 4 }, {
        default: () => [
          statusTag('STRM', status.strm_status),
          statusTag('Emby', status.emby_index_status),
          statusTag('神医', status.shenyi_persist_status),
          statusTag('媒体流', status.emby_media_status),
          h(NText, { depth: 3, style: 'font-size: 0.78em;' }, {
            default: () => `总结: ${status.summary_status || 'unknown'}`
          }),
          status.active_job ? h(NText, { depth: 3, style: 'font-size: 0.78em;' }, {
            default: () => `任务: ${status.active_job.state}${status.active_job.reason_code ? ` / ${status.active_job.reason_code}` : ''}`
          }) : null,
          status.retry_after ? h(NText, { type: 'warning', style: 'font-size: 0.78em;' }, {
            default: () => `冷却至: ${formatDate(status.retry_after)}`
          }) : null,
        ]
      });
    }
  },
  {
    title: '操作',
    key: 'actions',
    width: 520,
    align: 'center',
    fixed: 'right',
    render(row) {
      const actionButtons = [];
      
      // ✅ [修改] 将“重新处理”按钮移出条件判断，使其在搜索结果中也显示
      actionButtons.push(
        h(NButton, {
          size: 'small',
          type: 'info',
          ghost: true,
          disabled: !rowTargetId(row) || globalRecheck.value.running || loadingAction.value[`recheck-${row.item_id}`],
          loading: loadingAction.value[`recheck-${row.item_id}`],
          onClick: () => recheckMediaInfo(row)
        }, { default: () => '重新核对' })
      );

      const mediaStatus = mediaInfoStatuses.value[row.item_id];
      actionButtons.push(
        h(NPopconfirm, { onPositiveClick: () => repairMediaInfo(row) }, {
          trigger: () => h(NButton, {
            size: 'small',
            type: 'warning',
            ghost: true,
            loading: loadingAction.value[`repair-${row.item_id}`],
            disabled: !repairFeatureEnabled.value || !mediaStatus?.repair_eligible || loadingAction.value[`repair-${row.item_id}`]
          }, { default: () => '神医修复' }),
          default: () => h('div', { style: 'max-width: 420px;' },
            '将调用神医单 Item MediaInfo 接口，可能访问 STRM 对应远程媒体并由神医触发 ffprobe/rffmpeg；不会刷新整季、整剧或媒体库，EVH 不会自行执行 ffprobe。'
          )
        })
      );

      if (mediaStatus?.active_job?.state === 'pending') {
        actionButtons.push(
          h(NButton, {
            size: 'small',
            type: 'error',
            ghost: true,
            onClick: () => cancelMediaInfoJob(row)
          }, { default: () => '取消等待' })
        );
      }

      actionButtons.push(
        h(NPopconfirm, { onPositiveClick: () => handleReprocessItem(row) }, {
            trigger: () => h(NButton, {
                size: 'small',
                type: 'warning',
                ghost: true,
                loading: loadingAction.value[row.item_id] && currentRowId.value === row.item_id,
                disabled: loadingAction.value[row.item_id] || props.taskStatus?.is_running
            }, {
                icon: () => h(NIcon, { component: ReprocessIcon }),
                default: () => '重新处理'
            }),
            default: () => `确定要重新处理 "${row.item_name}" 吗？`
        })
      );

      actionButtons.push(
        h(NButton, {
          size: 'small',
          type: 'primary',
          onClick: () => goToEditPage(row)
        }, { default: () => '手动编辑' })
      );

      // “标记为已处理”按钮仅在待复核列表视图中显示
      if (!isShowingSearchResults.value) {
        actionButtons.push(
          h(NPopconfirm, { onPositiveClick: () => handleMarkAsProcessed(row) }, {
            trigger: () => h(NButton, {
              size: 'small',
              type: 'success',
              ghost: true,
              loading: loadingAction.value[row.item_id] && currentRowId.value === row.item_id,
              disabled: loadingAction.value[row.item_id] || props.taskStatus?.is_running
            }, {
              icon: () => h(NIcon, { component: MarkDoneIcon }),
            }),
            default: () => `确定要将 "${row.item_name}" 标记为已处理吗？`
          })
        );
      }
      
      if (row.item_type === 'Series') {
        actionButtons.push(
          h(NButton, {
            size: 'small',
            title: '添加到追剧列表',
            onClick: () => addToWatchlist(row)
          }, { icon: () => h(NIcon, { component: AddToWatchlistIcon }) })
        );
      }
      return h(NSpace, { justify: 'center' }, { default: () => actionButtons });
    }
  }
]);

// ... other functions like paginationProps, fetchReviewItems, etc. remain unchanged ...
const paginationProps = computed(() => ({
    disabled: isShowingSearchResults.value,
    page: currentPage.value,
    pageSize: itemsPerPage.value,
    itemCount: totalItems.value,
    showSizePicker: true,
    pageSizes: [10, 15, 20, 30, 50, 100],
    onChange: (page) => {
        currentPage.value = page;
        if (!isShowingSearchResults.value) {
            fetchReviewItems();
        }
    },
    onUpdatePageSize: (pageSize) => {
        itemsPerPage.value = pageSize;
        currentPage.value = 1;
        if (!isShowingSearchResults.value) {
            fetchReviewItems();
        }
    }
}));

const fetchReviewItems = async () => {
  const request = listRequestGate.begin('review-list');
  loading.value = true;
  error.value = null;
  isShowingSearchResults.value = false;
  try {
    const response = await axios.get(`/api/review_items`, {
        signal: request.signal,
        params: {
            page: currentPage.value,
            per_page: itemsPerPage.value,
        }
    });
    if (!request.commit(() => {
      tableData.value = response.data.items;
      totalItems.value = response.data.total_items;
      checkedRowKeys.value = [];
    })) return;
    await fetchMediaInfoStatuses(response.data.items);
  } catch (err) {
    if (!axios.isCancel(err) && request.isCurrent()) {
      handleFetchError(err, "加载待处理列表失败。");
    }
  } finally {
    request.commit(() => { loading.value = false; });
    request.finish();
  }
};

const searchEmbyLibrary = async () => {
  const request = listRequestGate.begin('review-list');
  loading.value = true;
  error.value = null;
  isShowingSearchResults.value = true;
  try {
    const response = await axios.get(`/api/search_emby_library`, {
        params: { query: searchQuery.value },
        signal: request.signal,
    });
    // 为搜索结果补充一些待复核列表才有的字段，防止渲染时出错
    const rows = response.data.items.map(item => ({
        ...item,
        failed_at: null,
        reason: 'N/A',
    }));
    if (!request.commit(() => {
      tableData.value = rows;
      totalItems.value = response.data.total_items;
      checkedRowKeys.value = [];
    })) return;
    await fetchMediaInfoStatuses(rows);
  } catch (err) {
    if (!axios.isCancel(err) && request.isCurrent()) {
      handleFetchError(err, "搜索 Emby 媒体库失败。");
    }
  } finally {
    request.commit(() => { loading.value = false; });
    request.finish();
  }
};

const handleReprocessItem = async (row) => {
  currentRowId.value = row.item_id;
  loadingAction.value[row.item_id] = true;
  try {
    // ★★★ 修改：在请求体中传递 reason ★★★
    const response = await axios.post(`/api/actions/reprocess_item/${row.item_id}`, {
        reason: row.reason // 将当前行的失败原因传给后端
    });
    message.success(response.data.message || `项目 "${row.item_name}" 的重新处理任务已提交。`);
    // 如果是在待复核列表操作，则刷新列表
    if (!isShowingSearchResults.value) {
        await fetchReviewItems();
    }
  } catch (err) {
    console.error("重新处理失败:", err);
    message.error(`操作失败: ${err.response?.data?.error || err.message}`);
  } finally {
    loadingAction.value[row.item_id] = false;
    currentRowId.value = null;
  }
};

const reprocessAllReviewItems = async () => {
  try {
    const response = await axios.post('/api/actions/reprocess_all_review_items');
    message.success(response.data.message || '重新处理所有待复核项的任务已成功提交！');
  } catch (err)
 {
    console.error("提交重新处理所有任务失败:", err);
    message.error(`操作失败: ${err.response?.data?.error || err.message}`);
  }
};

const handleFetchError = (err, defaultMessage) => {
    console.error(defaultMessage, err);
    error.value = defaultMessage + (err.response?.data?.error || err.message);
    message.error(error.value);
};

const handleSearch = () => {
  if (searchQuery.value.trim()) {
    currentPage.value = 1;
    searchEmbyLibrary();
  } else {
    currentPage.value = 1;
    fetchReviewItems();
  }
};

const fetchMediaInfoStatuses = async (rows) => {
  const eligibleRows = (rows || []).filter(row => rowTargetId(row));
  await Promise.allSettled(eligibleRows.map(async row => {
    const request = mediaInfoRequestGate.begin(row.item_id);
    try {
      const response = await axios.get(`/api/media-info/items/${rowTargetId(row)}/status`, {
        signal: request.signal,
      });
      request.commit(() => {
        mediaInfoStatuses.value = {
          ...mediaInfoStatuses.value,
          [row.item_id]: response.data,
        };
      });
    } finally {
      request.finish();
    }
  }));
};

const recheckMediaInfo = async (row) => {
  const targetId = rowTargetId(row);
  if (!targetId) return;
  const request = mediaInfoRequestGate.begin(row.item_id);
  const actionKey = `recheck-${row.item_id}`;
  loadingAction.value[actionKey] = request.generation;
  try {
    const response = await axios.post(`/api/media-info/items/${targetId}/recheck`, null, {
      signal: request.signal,
    });
    request.commit(() => {
      mediaInfoStatuses.value = { ...mediaInfoStatuses.value, [row.item_id]: response.data };
      message.success('媒体信息四层状态已重新核对。');
    });
  } catch (err) {
    if (!axios.isCancel(err) && request.isCurrent()) {
      message.error(err.response?.data?.error || '重新核对失败。');
    }
  } finally {
    if (loadingAction.value[actionKey] === request.generation) {
      loadingAction.value[actionKey] = false;
    }
    request.finish();
  }
};

const repairMediaInfo = async (row) => {
  const targetId = rowTargetId(row);
  if (!targetId) return;
  const actionKey = `repair-${row.item_id}`;
  if (loadingAction.value[actionKey]) return;
  const request = mediaInfoRequestGate.begin(row.item_id);
  loadingAction.value[actionKey] = request.generation;
  try {
    const response = await axios.post(`/api/media-info/items/${targetId}/repair`, null, {
      signal: request.signal,
    });
    if (!request.isCurrent()) return;
    message.success(response.data.result === 'existing' ? '该项目已有修复任务。' : '神医单项修复任务已提交。');
    loadingAction.value[actionKey] = false;
    request.finish();
    await fetchMediaInfoStatuses([row]);
  } catch (err) {
    if (!axios.isCancel(err) && request.isCurrent()) {
      message.error(err.response?.data?.reason_code || err.response?.data?.error || '修复任务提交失败。');
    }
  } finally {
    if (mediaInfoRequestGate.isActive() && loadingAction.value[actionKey] === request.generation) {
      loadingAction.value[actionKey] = false;
    }
    request.finish();
  }
};

const repairSelectedMediaInfo = async () => {
  if (batchRepairLoading.value || checkedRowKeys.value.length === 0 || checkedRowKeys.value.length > 20) return;
  const request = mediaInfoRequestGate.begin('__batch__');
  const selectedRows = tableData.value.filter(row => checkedRowKeys.value.includes(row.item_id));
  const selectedIds = [...new Set(selectedRows.map(rowTargetId).filter(Boolean))];
  selectedRows.forEach(row => mediaInfoRequestGate.invalidate(row.item_id));
  batchRepairLoading.value = true;
  try {
    const response = await axios.post('/api/media-info/repair-batch', {
      item_ids: selectedIds
    }, {
      signal: request.signal,
    });
    if (!request.isCurrent()) return;
    const data = response.data;
    message.success(`已接受 ${data.accepted.length}，跳过 ${data.skipped.length}，拒绝 ${data.rejected.length}。`);
    await fetchMediaInfoStatuses(selectedRows);
  } catch (err) {
    if (!axios.isCancel(err) && request.isCurrent()) {
      message.error(err.response?.data?.error || '批量修复提交失败。');
    }
  } finally {
    request.commit(() => { batchRepairLoading.value = false; });
    request.finish();
  }
};

const cancelMediaInfoJob = async (row) => {
  const jobId = mediaInfoStatuses.value[row.item_id]?.active_job?.id;
  if (!jobId) return;
  try {
    await axios.post(`/api/media-info/jobs/${jobId}/cancel`);
    message.success('等待中的任务已取消。');
    await fetchMediaInfoStatuses([row]);
  } catch (err) {
    message.error(err.response?.data?.error || '任务已开始，无法取消。');
  }
};

const refreshActiveMediaInfoJobs = async () => {
  const activeRows = tableData.value.filter(row => {
    const state = mediaInfoStatuses.value[row.item_id]?.active_job?.state;
    return ['pending', 'running', 'submitting', 'submitted'].includes(state);
  });
  if (activeRows.length) {
    await fetchMediaInfoStatuses(activeRows);
  }
};

const cancelRecheckAllMediaInfo = () => {
  if (!globalRecheck.value.running) return;
  globalRecheckGeneration += 1;
  globalRecheck.value = { ...globalRecheck.value, running: false, cancelled: true };
  globalRecheckController?.abort();
  globalRecheckController = null;
};

const recheckAllMediaInfo = async () => {
  if (globalRecheck.value.running) return;
  const generation = ++globalRecheckGeneration;
  globalRecheck.value = {
    running: true,
    total: 0,
    completed: 0,
    succeeded: 0,
    failed: 0,
    cancelled: false,
  };
  const controller = new AbortController();
  globalRecheckController = controller;
  const sourceRequests = new Map();
  try {
    const response = await axios.get('/api/media-info/review-targets', {
      params: { limit: 1000 },
      signal: controller.signal,
    });
    if (generation !== globalRecheckGeneration) return;
    if (response.data.truncated) {
      message.warning(`待复核记录超过 ${response.data.limit} 条，本次按安全上限处理前 ${response.data.limit} 条。`);
    }
    const targets = collectUniqueReviewTargets(response.data.targets || []);
    for (const target of targets) {
      for (const sourceId of target.sourceIds) {
        sourceRequests.set(sourceId, mediaInfoRequestGate.begin(sourceId));
      }
    }
    globalRecheck.value = { ...globalRecheck.value, total: targets.length };
    const summary = await runBoundedReadOnlyRecheck({
      entries: response.data.targets || [],
      concurrency: 4,
      signal: controller.signal,
      isCurrent: () => generation === globalRecheckGeneration,
      requestRecheck: async (targetId, signal) => {
        const result = await axios.post(`/api/media-info/items/${targetId}/recheck`, null, { signal });
        return result.data;
      },
      onResult: (target, result) => {
        const next = { ...mediaInfoStatuses.value };
        target.sourceIds.forEach(sourceId => {
          sourceRequests.get(sourceId)?.commit(() => { next[sourceId] = result; });
        });
        mediaInfoStatuses.value = next;
      },
      onProgress: progress => {
        if (generation !== globalRecheckGeneration) return;
        globalRecheck.value = { ...globalRecheck.value, ...progress };
      },
    });
    if (generation === globalRecheckGeneration) {
      globalRecheck.value = { ...globalRecheck.value, ...summary, running: false };
      message.success(`全部核对完成：${globalRecheck.value.succeeded} 成功，${globalRecheck.value.failed} 失败。`);
    }
  } catch (err) {
    if (!axios.isCancel(err) && generation === globalRecheckGeneration) {
      globalRecheck.value = { ...globalRecheck.value, running: false, failed: globalRecheck.value.failed + 1 };
      message.error(err.response?.data?.error || '加载全部核对目标失败。');
    }
  } finally {
    sourceRequests.forEach(request => request.finish());
    if (globalRecheckController === controller) globalRecheckController = null;
  }
};

onMounted(() => {
  fetchReviewItems();
  mediaInfoPollTimer = window.setInterval(refreshActiveMediaInfoJobs, 3000);
});

onBeforeUnmount(() => {
  if (mediaInfoPollTimer) window.clearInterval(mediaInfoPollTimer);
  mediaInfoRequestGate.dispose();
  listRequestGate.dispose();
  cancelRecheckAllMediaInfo();
});
</script>

<style scoped>
</style>
