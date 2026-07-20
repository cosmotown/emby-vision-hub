<!-- src/components/UserCenterPage.vue -->
<template>
  <section class="user-center-page">
    <n-page-header
      :title="`欢迎回来，${accountInfo?.name || authStore.username}`"
      subtitle="在这里查看您的账户信息"
    />

    <n-grid class="stats-grid" cols="2 s:3 l:5" :x-gap="12" :y-gap="12" responsive="screen">
      <n-gi><n-card size="small" :bordered="false" class="summary-card"><n-statistic label="总申请" :value="stats.total" /></n-card></n-gi>
      <n-gi><n-card size="small" :bordered="false" class="summary-card"><n-statistic label="已完成" :value="stats.completed" style="--n-value-text-color: var(--n-success-color)" /></n-card></n-gi>
      <n-gi><n-card size="small" :bordered="false" class="summary-card"><n-statistic label="处理中" :value="stats.processing" style="--n-value-text-color: var(--n-info-color)" /></n-card></n-gi>
      <n-gi><n-card size="small" :bordered="false" class="summary-card"><n-statistic label="待审核" :value="stats.pending" style="--n-value-text-color: var(--n-warning-color)" /></n-card></n-gi>
      <n-gi class="summary-last"><n-card size="small" :bordered="false" class="summary-card"><n-statistic label="未通过" :value="stats.failed" style="--n-value-text-color: var(--n-error-color)" /></n-card></n-gi>
    </n-grid>

    <n-grid class="user-center-grid" cols="1 m:2 xl:3" :x-gap="20" :y-gap="20" responsive="screen">
      <n-gi>
        <n-card :bordered="false" class="dashboard-card user-center-card">
          <template #header><span class="card-title">账户详情</span></template>
          <template #header-extra><n-spin v-if="loading" size="small" /></template>

          <div v-if="accountInfo" class="profile-layout">
            <div class="profile-avatar-section">
              <n-tooltip trigger="hover" placement="right">
                <template #trigger>
                  <button type="button" class="avatar-wrapper" aria-label="更换头像" @click="triggerFileUpload">
                    <n-avatar :size="88" :src="avatarUrl" object-fit="cover">
                      <span v-if="!avatarUrl">{{ usernameInitial }}</span>
                    </n-avatar>
                    <span class="avatar-overlay"><n-icon size="22"><CloudUploadOutline /></n-icon></span>
                  </button>
                </template>
                点击更换头像
              </n-tooltip>
              <input ref="fileInput" class="visually-hidden" type="file" accept="image/png, image/jpeg, image/jpg" @change="handleAvatarChange" />
              <div class="username-text">{{ accountInfo?.name || authStore.username }}</div>
              <n-tag :type="statusType" size="small" round>{{ statusText }}</n-tag>
            </div>

            <n-descriptions class="account-descriptions" label-placement="left" bordered :column="1" size="small">
              <n-descriptions-item label="注册时间">{{ formatDateTime(accountInfo.registration_date) }}</n-descriptions-item>
              <n-descriptions-item label="到期时间">{{ accountInfo.expiration_date ? formatDateTime(accountInfo.expiration_date) : '永久有效' }}</n-descriptions-item>
              <n-descriptions-item label="账户等级"><strong>{{ authStore.isAdmin ? '管理员' : (accountInfo.template_name || '未分配') }}</strong></n-descriptions-item>
              <n-descriptions-item label="等级说明">{{ authStore.isAdmin ? '拥有系统所有管理权限' : (accountInfo.template_description || '无') }}</n-descriptions-item>
              <n-descriptions-item label="订阅权限">
                <n-tag :type="authStore.isAdmin || accountInfo.allow_unrestricted_subscriptions ? 'success' : 'warning'" size="small">
                  {{ authStore.isAdmin || accountInfo.allow_unrestricted_subscriptions ? '免审核订阅' : '需管理员审核' }}
                </n-tag>
              </n-descriptions-item>
              <n-descriptions-item label="通知 ID">
                <div class="notification-id-control">
                  <n-input v-model:value="telegramChatId" placeholder="Telegram Chat ID" size="small" />
                  <n-button type="primary" secondary :loading="isSavingChatId" size="small" @click="saveChatId">保存</n-button>
                </div>
              </n-descriptions-item>
              <n-descriptions-item v-if="accountInfo.telegram_channel_id" label="全局通知">
                <n-button text type="primary" tag="a" :href="globalChannelLink" target="_blank" size="small">加入频道/群组</n-button>
              </n-descriptions-item>
            </n-descriptions>
            <n-button text type="primary" size="small" :loading="isFetchingBotLink" @click="openBotChat">联系机器人并发送 /start</n-button>
          </div>
          <n-empty v-else-if="!loading" description="账户信息暂不可用" />
        </n-card>
      </n-gi>

      <n-gi>
        <n-card :bordered="false" class="dashboard-card user-center-card">
          <template #header><span class="card-title">播放记录</span></template>
          <template #header-extra>
            <n-radio-group v-model:value="playbackFilter" size="small" class="card-filter" @update:value="handleFilterChange">
              <n-radio-button value="all">全部</n-radio-button>
              <n-radio-button value="Movie">电影</n-radio-button>
              <n-radio-button value="Episode">剧集</n-radio-button>
              <n-radio-button value="Audio">音乐</n-radio-button>
            </n-radio-group>
          </template>

          <n-grid :cols="2" class="playback-summary">
            <n-gi><n-statistic label="近期观看" :value="playbackData?.personal?.total_count || 0" suffix="次" /></n-gi>
            <n-gi><n-statistic label="累计时长" :value="playbackHours" suffix="小时" /></n-gi>
          </n-grid>
          <n-spin :show="playbackLoading">
            <n-scrollbar class="history-scroll">
              <n-list v-if="playbackData?.personal?.history_list?.length" hoverable size="small">
                <n-list-item v-for="(item, index) in playbackData.personal.history_list" :key="`${item.date}-${index}`">
                  <n-thing :title="item.title" content-style="margin-top: 0;">
                    <template #description><span class="item-meta">{{ formatDate(item.date) }} · {{ item.duration }} 分钟</span></template>
                    <template #header-extra><n-tag :type="getTypeTagColor(item.item_type)" size="tiny" round>{{ ITEM_TYPE_MAP[item.item_type] || item.item_type }}</n-tag></template>
                  </n-thing>
                </n-list-item>
              </n-list>
              <n-empty v-else description="暂无播放记录" />
            </n-scrollbar>
          </n-spin>
        </n-card>
      </n-gi>

      <n-gi>
        <n-card :bordered="false" class="dashboard-card user-center-card">
          <template #header><span class="card-title">订阅历史</span></template>
          <template #header-extra>
            <n-radio-group v-model:value="filterStatus" size="small" class="card-filter">
              <n-radio-button value="all">全部</n-radio-button>
              <n-radio-button value="completed">已完成</n-radio-button>
              <n-radio-button value="processing">处理中</n-radio-button>
            </n-radio-group>
          </template>

          <n-scrollbar class="history-scroll">
            <n-list v-if="subscriptionHistory.length" size="small">
              <n-list-item v-for="item in subscriptionHistory" :key="item.id">
                <div class="history-item-header">
                  <span class="history-title">{{ item.title }}</span>
                  <n-tag :type="getStatusType(item.status)" size="tiny" :bordered="false">{{ getStatusText(item.status) }}</n-tag>
                </div>
                <div class="item-meta">{{ formatDate(item.requested_at) }} · {{ item.item_type === 'Movie' ? '电影' : '剧集' }}</div>
                <div v-if="item.notes" class="history-item-notes">备注：{{ item.notes }}</div>
              </n-list-item>
            </n-list>
            <n-empty v-else description="暂无订阅记录" />
          </n-scrollbar>
          <div v-if="totalRecords > pageSize" class="pagination-wrap">
            <n-pagination v-model:page="currentPage" :page-size="pageSize" :item-count="totalRecords" simple @update:page="fetchSubscriptionHistory" />
          </div>
        </n-card>
      </n-gi>
    </n-grid>
  </section>
</template>

<script setup>
import { ref, onMounted, computed, watch } from 'vue';
import axios from 'axios';
import { CloudUploadOutline } from '@vicons/ionicons5';
import { useAuthStore } from '../stores/auth';
import { 
  NPageHeader, NCard, NDescriptions, NDescriptionsItem, NTag, NEmpty, NGrid, NGi, 
  NInput, NButton, useMessage, NPagination,
  NStatistic, NRadioGroup, NRadioButton, NAvatar, NIcon, NTooltip, NSpin,
  NList, NListItem, NThing, NScrollbar
} from 'naive-ui';

const authStore = useAuthStore();
const loading = ref(true);
const accountInfo = ref(null);
const subscriptionHistory = ref([]);
const telegramChatId = ref('');
const isSavingChatId = ref(false);
const message = useMessage();
const isFetchingBotLink = ref(false);
const playbackData = ref(null);
const playbackFilter = ref('all');
const playbackLoading = ref(false);
// 分页相关状态
const currentPage = ref(1);
const pageSize = ref(10); 
const totalRecords = ref(0);
const stats = ref({ total: 0, completed: 0, processing: 0, pending: 0, failed: 0 });
const filterStatus = ref('all');
const fileInput = ref(null);

const avatarUrl = computed(() => {
  const tag = accountInfo.value?.profile_image_tag;
  const userId = accountInfo.value?.id;
  if (userId && tag) {
    return `/image_proxy/Users/${userId}/Images/Primary?tag=${tag}`;
  }
  return null;
});
const usernameInitial = computed(() => (authStore.username || 'U').charAt(0).toUpperCase());
const playbackHours = computed(() => Number((Number(playbackData.value?.personal?.total_minutes || 0) / 60).toFixed(1)));
const formatDate = (value) => value ? new Date(value).toLocaleDateString() : '—';
const formatDateTime = (value) => value ? new Date(value).toLocaleString() : '—';

const triggerFileUpload = () => {
  fileInput.value?.click();
};

const handleAvatarChange = async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  if (!['image/jpeg', 'image/png', 'image/jpg'].includes(file.type)) {
    message.error('只支持 JPG/PNG 格式的图片');
    return;
  }
  const formData = new FormData();
  formData.append('avatar', file);
  const loadingMsg = message.loading('正在上传头像...', { duration: 0 });
  try {
    const res = await axios.post('/api/portal/upload-avatar', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    });
    loadingMsg.destroy();
    message.success('头像更新成功！');
    if (accountInfo.value && res.data.new_tag) {
      accountInfo.value.profile_image_tag = res.data.new_tag;
    }
  } catch (error) {
    loadingMsg.destroy();
    message.error(error.response?.data?.message || '上传失败');
  } finally {
    event.target.value = ''; 
  }
};

const statusMap = {
  active: { text: '正常', type: 'success' },
  pending: { text: '待审批', type: 'warning' },
  expired: { text: '已过期', type: 'error' },
  disabled: { text: '已禁用', type: 'error' },
};

const statusText = computed(() => statusMap[accountInfo.value?.status]?.text || '未知');
const statusType = computed(() => statusMap[accountInfo.value?.status]?.type || 'default');

const globalChannelLink = computed(() => {
  if (!accountInfo.value || !accountInfo.value.telegram_channel_id) return '#';
  const channelId = accountInfo.value.telegram_channel_id.trim();
  if (channelId.startsWith('https://t.me/')) return channelId;
  if (channelId.startsWith('@')) return `https://t.me/${channelId.substring(1)}`;
  return `https://t.me/${channelId}`;
});

// 状态辅助函数 (供 PC 和 Mobile 共用)
const getStatusInfo = (status) => {
  const map = {
    completed: { type: 'success', text: '已完成' },
    WANTED: { type: 'info', text: '处理中' }, 
    REQUESTED: { type: 'warning', text: '待审核' },
    IGNORED: { type: 'error', text: '已忽略' },
    SUBSCRIBED: { type: 'info', text: '已订阅' }, 
    PENDING_RELEASE: { type: 'error', text: '未上映' },
    NONE: { type: 'warning', text: '已取消' },
    PAUSED: { type: 'warning', text: '已暂停' },
  };
  return map[status] || { type: 'default', text: status };
};

const getStatusType = (status) => getStatusInfo(status).type;
const getStatusText = (status) => getStatusInfo(status).text;

const saveChatId = async () => {
  isSavingChatId.value = true;
  try {
    const response = await axios.post('/api/portal/telegram-chat-id', { 
      chat_id: telegramChatId.value 
    });
    message.success(response.data.message || '保存成功！');
  } catch (error) {
    message.error(error.response?.data?.message || '保存失败');
  } finally {
    isSavingChatId.value = false;
  }
};

const openBotChat = async () => {
  isFetchingBotLink.value = true;
  try {
    const response = await axios.get('/api/portal/telegram-bot-info');
    const botName = response.data.bot_username;
    if (botName) {
      window.open(`https://t.me/${botName}`, '_blank');
    } else {
      const errorMsg = response.data.error || '未能获取到机器人信息';
      message.error(errorMsg, { duration: 8000 });
    }
  } catch (error) {
    message.error('请求机器人信息失败');
  } finally {
    isFetchingBotLink.value = false;
  }
};

const fetchStats = async () => {
  try {
    const res = await axios.get('/api/portal/subscription-stats');
    stats.value = res.data;
  } catch (e) {
    console.error("获取统计失败", e);
  }
};

const fetchSubscriptionHistory = async (page = 1) => {
  loading.value = true;
  try {
    const response = await axios.get('/api/portal/subscription-history', {
      params: {
        page: page,
        page_size: pageSize.value,
        status: filterStatus.value,
      },
    });
    subscriptionHistory.value = response.data.items;
    totalRecords.value = response.data.total_records;
    currentPage.value = page;
  } catch (error) {
    message.error('加载订阅历史失败');
  } finally {
    loading.value = false;
  }
};

// 1. 定义常量映射表
const ITEM_TYPE_MAP = {
  Movie: '电影',
  Episode: '剧集',
  Audio: '音乐',
  Video: '视频'
};

const getTypeTagColor = (type) => {
    switch(type) {
        case 'Movie': return 'info';
        case 'Episode': return 'success';
        case 'Audio': return 'warning';
        case 'Video': return 'error';
        default: return 'default';
    }
};

// 获取播放统计
const fetchPlaybackStats = async () => {
  playbackLoading.value = true;
  try {
    // 传入 media_type 参数
    const res = await axios.get(`/api/portal/playback-report?days=30&media_type=${playbackFilter.value}`);
    playbackData.value = res.data;
  } catch (error) {
    console.error("获取播放数据失败", error);
    message.error("获取播放统计失败");
  } finally {
    playbackLoading.value = false;
  }
};

// 筛选变更处理
const handleFilterChange = () => {
    fetchPlaybackStats();
};

watch(filterStatus, () => {
  fetchSubscriptionHistory(1); 
});

onMounted(async () => {
  try {
    const [accountResponse] = await Promise.all([
      axios.get('/api/portal/account-info'),
    ]);
    accountInfo.value = accountResponse.data;
    if (accountInfo.value) {
        telegramChatId.value = accountInfo.value.telegram_chat_id || '';
    }
    fetchStats();
    await fetchSubscriptionHistory();
  } catch (error) {
    message.error('加载账户信息失败');
  } finally {
    loading.value = false;
  }
  fetchPlaybackStats();
});
</script>

<style scoped>
.user-center-page {
  width: 100%;
  min-width: 0;
  padding: 24px;
}

.stats-grid,
.user-center-grid {
  margin-top: 20px;
}

.summary-card {
  height: 100%;
  text-align: center;
  background: var(--app-surface);
}

.user-center-card {
  min-height: 590px;
}

.profile-layout {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 18px;
}
.profile-avatar-section {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-bottom: 10px;
}
.username-text {
  margin-top: 12px;
  font-weight: 700;
  font-size: 1.2em;
  text-align: center;
  word-break: break-all;
}
.avatar-wrapper {
  position: relative;
  border-radius: 50%;
  overflow: hidden;
  transition: transform 0.2s;
  box-shadow: 0 4px 12px rgba(0,0,0,0.1);
  display: flex;
  justify-content: center;
  align-items: center;
  line-height: 0;
  width: 88px;
  height: 88px;
  margin: 0 auto;
  padding: 0;
  border: 0;
  color: #fff;
  background: transparent;
  cursor: pointer;
}
.avatar-wrapper:hover { transform: scale(1.05); }
.avatar-overlay {
  position: absolute;
  top: 0; left: 0; width: 100%; height: 100%;
  background-color: rgba(0, 0, 0, 0.4);
  display: flex;
  justify-content: center;
  align-items: center;
  opacity: 0;
  transition: opacity 0.2s;
  border-radius: 50%;
}
.avatar-wrapper :deep(img) { display: block !important; width: 100%; height: 100%; object-fit: cover; }
.avatar-wrapper:hover .avatar-overlay { opacity: 1; }

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

.account-descriptions,
.notification-id-control {
  width: 100%;
  min-width: 0;
}

.account-descriptions :deep(td),
.account-descriptions :deep(.n-descriptions-table-content) {
  min-width: 0;
  overflow-wrap: anywhere;
}

.notification-id-control {
  display: flex;
  gap: 8px;
}

.notification-id-control .n-input { min-width: 0; }

.user-center-grid :deep(.n-grid-item) {
  min-width: 0;
}

.user-center-grid :deep(.dashboard-card > .n-card-header) {
  flex-wrap: wrap;
  gap: 10px 12px;
}

.user-center-grid :deep(.dashboard-card > .n-card-header .n-card-header__main) { min-width: 0; }

.user-center-grid :deep(.dashboard-card > .n-card-header .n-card-header__extra) {
  min-width: 0;
  margin-left: auto;
}

.card-filter {
  max-width: 100%;
  flex-wrap: wrap;
}

.playback-summary {
  margin-bottom: 16px;
  text-align: center;
}

.history-scroll { max-height: 480px; }
.history-item-header { display: flex; min-width: 0; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.history-title { font-weight: 600; font-size: 14px; flex: 1; margin-right: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item-meta { font-size: 12px; color: var(--app-text-muted); }
.history-item-notes { margin-top: 6px; font-size: 12px; color: var(--n-text-color-3); background: rgba(0,0,0,0.03); padding: 4px; border-radius: 4px; }
.pagination-wrap { display: flex; justify-content: center; margin-top: 14px; }

html.dark .mobile-history-item { background-color: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.05); }
html.dark .history-item-notes { background: rgba(255,255,255,0.05); }

@media (max-width: 767px) {
  .user-center-page { padding: 14px 12px; }
  .stats-grid,
  .user-center-grid { margin-top: 14px; }
  .stats-grid > .summary-last { grid-column: 1 / -1 !important; }
  .user-center-card { min-height: auto; }
  .user-center-grid :deep(.dashboard-card > .n-card-header) { align-items: flex-start; }
  .user-center-grid :deep(.dashboard-card > .n-card-header .n-card-header__extra) {
    width: 100%;
    margin-left: 0;
  }
  .card-filter {
    display: grid !important;
    width: 100%;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  .card-filter :deep(.n-radio-group__splitor) { display: none; }
  .card-filter :deep(.n-radio-button) { width: 100%; }
  .card-filter :deep(.n-radio-button__label) { width: 100%; padding: 0 8px; text-align: center; }
  .card-filter :deep(.n-radio-button) { width: 100%; text-align: center; }
  .notification-id-control { flex-wrap: wrap; }
  .notification-id-control .n-button { width: 100%; }
  .history-scroll { max-height: none; }
}
</style>
