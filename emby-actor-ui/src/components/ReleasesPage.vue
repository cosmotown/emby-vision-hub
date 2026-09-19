<template>
  <n-layout content-style="padding: 24px;">
    <n-page-header title="查看更新">
      <template #extra>
        <n-tooltip>
          <template #trigger>
            <n-button @click="showSponsorModal = true" type="primary" ghost>
              <template #icon><n-icon :component="CafeIcon" /></template>
              支持 CosmoTown
            </n-button>
          </template>
          您的支持是 CosmoTown 持续维护项目的动力。
        </n-tooltip>
        <n-button tag="a" :href="`https://github.com/${githubRepo}/issues`" target="_blank" secondary>
          <template #icon><n-icon :component="LogoGithub" /></template>
          反馈问题
        </n-button>
        
        <n-button
          type="success"
          @click="handleUpdate"
          :loading="isUpdating"
          :disabled="isUpdating"
        >
          {{ appStore.isUpdateAvailable ? '立即更新' : '检查并更新' }}
        </n-button>
      </template>
    </n-page-header>
    <n-divider />

    <n-alert title="开源许可与免责声明" type="info" :bordered="false" class="legal-notice">
      EVH 依据 AGPL-3.0 发布，源代码与修改历史可在
      <a :href="`https://github.com/${githubRepo}`" target="_blank">项目仓库</a>
      查看。本软件不提供任何明示或默示担保，也不是 Emby、MoviePilot 或其他第三方服务的官方产品。
    </n-alert>

    <div v-if="isLoading" class="dashboard-card"><n-spin size="large" /></div>
    <div v-else-if="error" class="center-container"><n-alert title="加载错误" type="error">{{ error }}</n-alert></div>
    
    <div v-else>
      <n-list hoverable clickable>
        <n-list-item v-for="release in appStore.releases" :key="release.version">
          <n-thing>
            <template #header>
              <n-space align="center">
                <a :href="release.url" target="_blank" class="version-link">{{ release.version }}</a>
                <n-tag v-if="isLatestStable(release.version)" type="success" size="small" round>最新软件版本</n-tag>
                <n-tag v-if="isCurrentRelease(release.version)" type="info" size="small" round>当前版本</n-tag>
              </n-space>
            </template>
            <template #header-extra>
              <n-text :depth="3">{{ formatReleaseDate(release.published_at) }}</n-text>
            </template>
            <div class="changelog-content" v-html="renderMarkdown(release.changelog)"></div>
          </n-thing>
        </n-list-item>
      </n-list>
    </div>

    <!-- 支持开发者 模态框 -->
    <n-modal v-model:show="showSponsorModal" preset="card" style="width: 90%; max-width: 400px;" title="支持 CosmoTown" :bordered="false">
      <div class="sponsor-content">
        <n-p>
          感谢支持 CosmoTown 继续维护 EVH。
        </n-p>
        <n-p>
          您的每一份支持，都会用于后续功能开发、测试与文档维护。
        </n-p>
        <n-divider />
        <div class="qr-code-item">
          <n-image width="200" src="/img/wechat_pay.png" />
          <n-text strong style="margin-top: 10px;">推荐使用微信支付</n-text>
        </div>
      </div>
    </n-modal>

    <!-- ▼▼▼【优化后】更新进度模态框 ▼▼▼ -->
    <n-modal
      v-model:show="showUpdateModal"
      :mask-closable="false"
      preset="card"
      title="正在更新应用"
      style="width: 90%; max-width: 500px;"
    >
      <n-space align="center" style="margin-top: 20px; margin-bottom: 20px;">
        <!-- 动态加载动画 -->
        <n-spin v-if="isUpdating" size="small" />
        <!-- 状态文本 -->
        <n-text>{{ updateStatusText }}</n-text>
      </n-space>
      <n-text v-if="updateTransactionId" depth="3" style="font-size: 12px;">
        事务 ID：{{ updateTransactionId }}
      </n-text>

      <template #footer>
        <div style="text-align: right;">
          <n-button @click="closeUpdateModal" :disabled="!isUpdateFinished">
            关闭
          </n-button>
        </div>
      </template>
    </n-modal>
    <!-- ▲▲▲ 优化结束 ▲▲▲ -->

  </n-layout>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue';
import axios from 'axios';
import { marked } from 'marked';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { zhCN } from 'date-fns/locale';
import { 
  NLayout, NPageHeader, NDivider, NSpin, NAlert, NList, NListItem, NThing, 
  NTag, NSpace, NButton, NIcon, NText, NModal, NTooltip, useDialog,
  NImage, NP // 确保导入了 NImage 和 NP
} from 'naive-ui';
import { LogoGithub, CafeOutline as CafeIcon } from '@vicons/ionicons5';
import { useAppStore } from '../stores/app';

const dialog = useDialog();
const appStore = useAppStore();

const githubRepoOwner = 'cosmotown';
const githubRepoName = 'emby-vision-hub';
const githubRepo = computed(() => `${githubRepoOwner}/${githubRepoName}`);

const isLoading = ref(false);
const error = ref(null);
const showSponsorModal = ref(false);

// --- 更新状态相关的响应式变量 ---
const isUpdating = ref(false);
const showUpdateModal = ref(false);
const updateStatusText = ref('');
const isUpdateFinished = ref(false);
const updateTransactionId = ref(localStorage.getItem('evhUpdateTransactionId') || '');
let pollTimer = null;

const terminalStates = new Set(['SUCCESS', 'ALREADY_CURRENT', 'ROLLED_BACK', 'FAILED', 'AMBIGUOUS']);
const stateLabels = {
  PREPARING: '正在执行更新预检…',
  PULLING: '正在拉取并校验目标正式镜像…',
  TARGET_PINNED: '目标镜像身份已固定…',
  AMBIGUOUS: '操作结果无法安全确认，已停止自动更新，请通过原部署管理器恢复。',
  RECREATING: '正在事务性替换 EVH 容器…',
  STARTING: '新 EVH 容器正在启动…',
  HEALTH_CHECK: '正在等待新容器健康检查…',
  VERIFYING: '正在核验镜像、版本和运行配置…',
  SUCCESS: '更新完成，所有提交条件均已通过。',
  ALREADY_CURRENT: '当前容器已经运行目标镜像和目标版本，无需更新。',
  ROLLING_BACK: '新版本未通过验证，正在恢复旧版本…',
  ROLLED_BACK: '更新失败，旧版本已恢复并通过健康检查。',
  FAILED: '更新失败，未能完成事务。',
};

const stopPolling = () => {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
};

const closeUpdateModal = () => {
  showUpdateModal.value = false;
  if (isUpdateFinished.value) {
    localStorage.removeItem('evhUpdateTransactionId');
    updateTransactionId.value = '';
  }
};

const schedulePoll = (delay = 2000) => {
  stopPolling();
  pollTimer = setTimeout(pollUpdateStatus, delay);
};

const pollUpdateStatus = async () => {
  if (!updateTransactionId.value) return;
  try {
    const response = await axios.get(`/api/system/update/status/${updateTransactionId.value}`);
    const transaction = response.data;
    const stateText = transaction.message || stateLabels[transaction.state] || transaction.state;
    const failureDetail = ['ROLLED_BACK', 'FAILED', 'AMBIGUOUS'].includes(transaction.state) && transaction.last_error
      ? ` 原因：${transaction.last_error}`
      : '';
    updateStatusText.value = `${stateText}${failureDetail}`;
    if (terminalStates.has(transaction.state)) {
      isUpdateFinished.value = true;
      isUpdating.value = false;
      stopPolling();
      await appStore.fetchVersionInfo();
      return;
    }
    isUpdating.value = true;
    schedulePoll();
  } catch (err) {
    updateStatusText.value = '服务正在重启或暂时不可达，正在重新连接更新事务…';
    isUpdating.value = true;
    schedulePoll(3000);
  }
};

const handleUpdate = () => {
  dialog.warning({
    title: '确认更新',
    content: '将拉取 EVH 最新稳定镜像并重启应用，期间服务将短暂中断。确定要继续吗？',
    positiveText: '检查并更新',
    negativeText: '取消',
    onPositiveClick: () => {
      // 重置状态
      showUpdateModal.value = true;
      isUpdateFinished.value = false;
      isUpdating.value = true;
      updateStatusText.value = '正在创建持久化更新事务…';
      axios.post('/api/system/update/start').then((response) => {
        updateTransactionId.value = response.data.transaction_id;
        localStorage.setItem('evhUpdateTransactionId', updateTransactionId.value);
        updateStatusText.value = response.data.message || stateLabels[response.data.state];
        schedulePoll(500);
      }).catch((err) => {
        updateStatusText.value = err.response?.data?.error || '无法启动更新事务。';
        isUpdateFinished.value = true;
        isUpdating.value = false;
      });
    },
  });
};

const fetchData = async () => {
  isLoading.value = true;
  error.value = null;
  try {
    await appStore.fetchVersionInfo();
  } catch (err) {
    error.value = '无法获取版本信息，请检查网络或后端服务。';
  } finally {
    isLoading.value = false;
  }
};

const renderMarkdown = (markdownText) => {
  if (!markdownText) return '';
  return marked.parse(markdownText, { gfm: true, breaks: true });
};

const normalizeVersion = (version) => (version || '').replace(/^v/i, '').trim();

const isCurrentRelease = (releaseVersion) => {
  return normalizeVersion(releaseVersion) === normalizeVersion(appStore.currentVersion);
};

const isLatestStable = (releaseVersion) => {
  return normalizeVersion(releaseVersion) === normalizeVersion(appStore.latestVersion);
};

const formatReleaseDate = (dateString) => {
  if (!dateString) return '';
  return formatDistanceToNow(parseISO(dateString), { addSuffix: true, locale: zhCN });
};

onMounted(async () => {
  await fetchData();
  if (updateTransactionId.value) {
    showUpdateModal.value = true;
    isUpdating.value = true;
    isUpdateFinished.value = false;
    pollUpdateStatus();
  }
});
onUnmounted(stopPolling);
</script>

<style scoped>
.center-container {
  display: flex;
  justify-content: center;
  align-items: center;
  height: calc(100vh - 200px);
}
.version-link {
  font-size: 1.2em;
  font-weight: 600;
  color: var(--n-text-color);
  text-decoration: none;
}
.version-link:hover {
  text-decoration: underline;
}
.changelog-content {
  margin-top: 8px;
  padding-left: 4px;
  color: var(--n-text-color-2);
}
.changelog-content :deep(ul) {
  padding-left: 20px;
  margin: 0;
}
.changelog-content :deep(li) {
  margin-bottom: 4px;
}
.changelog-content :deep(pre) {
  background-color: rgba(128, 128, 128, 0.1);
  padding: 12px 16px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 10px 0;
}
.changelog-content :deep(code) {
  font-family: Consolas, Monaco, 'Andale Mono', 'Ubuntu Mono', monospace;
  font-size: 0.9em;
}
.sponsor-content {
  text-align: center;
}
.legal-notice {
  margin-bottom: 20px;
}
.legal-notice a {
  color: var(--app-primary);
  font-weight: 600;
}
.qr-code-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding-top: 10px;
}
</style>
