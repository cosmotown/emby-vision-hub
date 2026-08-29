<template>
  <n-layout class="person-cleanup-page" content-style="padding: 24px;">
    <n-page-header class="person-cleanup-header">
      <template #title>
        <n-space align="center">
          <span>人物清理</span>
          <n-tag type="warning" round :bordered="false" size="small">
            {{ candidates.length }} 位待复核
          </n-tag>
        </n-space>
      </template>
      <template #extra>
        <n-space class="person-cleanup-actions">
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
            type="warning"
            :loading="safeCleanupJob?.state === 'previewing'"
            :disabled="isBackgroundBusy || protectionSnapshot.state !== 'ready'"
            @click="startSafeCleanupPreview"
          >
            一键安全清理
          </n-button>
          <n-button
            v-if="safeCleanupJob?.job_id"
            secondary
            @click="openLatestSafeCleanupJob"
          >
            最近预览
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
      扫描只生成候选，不会删除人物。只有当前保护快照下显式核验为 orphan 的人物才能勾选；identity_alias_only 始终受保护。删除前还会再次查询 Emby；发现关联作品、连接失败、响应异常或 People 仍不可用都会跳过。删除接口需要神医 Pro 支持。
    </n-alert>

    <n-alert
      v-if="protectionSnapshot.state !== 'ready'"
      type="error"
      title="保护快照尚未就绪，人物清理已锁定"
      style="margin-bottom: 16px;"
    >
      当前状态：{{ protectionSnapshot.state || 'unknown' }}。必须先完成一次严格只读扫描，且所有受保护媒体库的分页、People 与人物详情均完整，才允许核对或删除。
    </n-alert>

    <section class="protected-libraries-panel">
      <n-space justify="space-between" align="center" style="margin-bottom: 12px;">
        <div>
          <n-text strong>受保护（跳过清理）的媒体库</n-text>
          <n-text depth="3" style="display:block;font-size:13px;margin-top:4px;">
            保存后必须执行一次只读扫描建立快照。库中出现过的人物及其同名重复记录会持续受保护，即使没有 TMDb ID 或以后失去作品关联，也不会进入清理候选。
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
                <n-tag v-if="library.protected_name_count" size="small" :bordered="false" type="info">
                  姓名键 {{ library.protected_name_count }} 个
                </n-tag>
                <n-tag v-if="library.protected_identity_count" size="small" :bordered="false" type="success">
                  外部身份 {{ library.protected_identity_count }} 个
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

    <n-alert
      v-if="readonlyScan"
      :type="readonlyScan.state === 'completed' ? 'success' : (readonlyScan.last_error ? 'warning' : 'info')"
      :title="readonlyScanTitle"
      style="margin-bottom: 16px;"
    >
      <template v-if="readonlyScan.state === 'completed'">
        保护别名核验 {{ readonlyScan.checked_count || 0 }}/{{ readonlyScan.candidate_total || 0 }}；
        本轮新增保护 {{ readonlyScan.protected_count || 0 }}；
        待人工复核 {{ candidates.length }}。
      </template>
      <template v-else>
        保护别名核验 {{ readonlyScan.checked_count || 0 }}/{{ readonlyScan.candidate_total || 0 }}；
        待核验 {{ readonlyScan.pending_count || 0 }}；
        本轮新增保护 {{ readonlyScan.protected_count || 0 }}。
        <span v-if="readonlyScan.last_error">原因：{{ readonlyScan.last_error }}</span>
      </template>
    </n-alert>

    <n-space align="center" style="margin: 16px 0 10px;">
      <n-text strong>全服务器幽灵人物候选</n-text>
      <n-text depth="3">已排除当前在用人物、保护库快照身份，以及只读核验确认的保护库别名人物。</n-text>
    </n-space>

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
        class="person-verify-card"
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
            :type="verificationAlertType"
            :title="verificationAlertTitle"
            style="margin-bottom: 16px;"
          >
            {{ verificationResult.message }}
          </n-alert>

          <n-descriptions bordered :column="1" label-placement="left" style="margin-bottom: 16px;">
            <n-descriptions-item label="Emby ID">{{ verificationResult.person_id }}</n-descriptions-item>
            <n-descriptions-item label="外部 ID">{{ providerIdText(verificationResult.provider_ids) }}</n-descriptions-item>
            <n-descriptions-item label="核对结果">
              {{ verificationSummary }}
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

          <div v-if="verificationResult.unverified_items?.length">
            <n-divider>无法核验的人物明细</n-divider>
            <n-alert type="warning" title="以下作品已返回，但 People 明细仍不可用" style="margin-bottom: 10px;">
              这些作品不会被当作“无关联”；当前人物保持受保护状态。
            </n-alert>
            <n-list bordered>
              <n-list-item v-for="item in verificationResult.unverified_items" :key="`unverified-${item.id}`">
                {{ item.series_name || item.name }}
              </n-list-item>
            </n-list>
          </div>

          <div v-if="verificationResult.status === 'orphan'">
            <n-divider>TMDb / IMDb / 豆瓣同身份对照</n-divider>
            <n-alert
              v-if="verificationResult.identity_comparison === 'unavailable'"
              type="warning"
              title="缺少外部身份"
            >
              该候选没有 TMDb、IMDb 或豆瓣 ID，无法查找 Emby 中的同身份人物，请结合姓名和头像人工判断。
            </n-alert>
            <n-alert
              v-else-if="verificationResult.identity_comparison === 'no_match'"
              type="info"
              title="没有同身份人物"
            >
              已按 TMDb/IMDb/豆瓣精确查询，Emby 中没有找到其他同身份 Person 记录。
            </n-alert>
            <template v-else>
              <n-alert type="info" title="发现同身份人物" style="margin-bottom: 12px;">
                以下人物与当前候选拥有相同 TMDb/IMDb/豆瓣身份，仅作为人工判断依据；不会自动删除或撤销当前候选。
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

    <n-modal v-model:show="safeCleanupModalVisible" :mask-closable="false">
      <n-card class="person-verify-card" title="一键安全清理" closable @close="safeCleanupModalVisible = false">
        <n-alert type="warning" title="持久化安全任务" style="margin-bottom: 16px;">
          预览不会删除人物。确认后将串行执行；每位人物删除前都会刷新完整保护快照并实时核验，且删除尝试必须先持久化后才发送一次 POST。
        </n-alert>
        <n-descriptions v-if="safeCleanupJob" bordered :column="1" label-placement="left">
          <n-descriptions-item label="状态">{{ safeCleanupJob.state }}</n-descriptions-item>
          <n-descriptions-item label="候选">{{ safeCleanupJob.candidate_total || 0 }}</n-descriptions-item>
          <n-descriptions-item label="显式 orphan">{{ safeCleanupJob.verified_orphan_count || 0 }}</n-descriptions-item>
          <n-descriptions-item label="受保护/跳过">{{ safeCleanupJob.protected_count || 0 }} / {{ safeCleanupJob.skipped_count || 0 }}</n-descriptions-item>
          <n-descriptions-item label="核验失败/删除失败">{{ safeCleanupJob.verification_failed_count || 0 }} / {{ safeCleanupJob.failed_count || 0 }}</n-descriptions-item>
          <n-descriptions-item v-if="safeCleanupJob.last_error" label="错误">{{ safeCleanupJob.last_error }}</n-descriptions-item>
        </n-descriptions>
        <template v-if="previewStateRows.length">
          <n-divider>核验结果明细</n-divider>
          <n-alert
            v-if="safeCleanupJob.preview_summary?.consistency_warning"
            type="error"
            title="持久化预览数据不一致"
            style="margin-bottom: 12px;"
          >
            {{ safeCleanupJob.preview_summary.consistency_warning }}
          </n-alert>
          <n-alert type="info" style="margin-bottom: 12px;">
            “核验失败”不等于“不是幽灵人物”，而是当前证据不足以授予删除资格。
            以下统计只读取本次历史 preview 已持久化的结果，不会重新访问 Emby。
          </n-alert>
          <n-list bordered>
            <n-list-item v-for="row in previewStateRows" :key="row.status">
              <n-space justify="space-between" align="center" :wrap="false">
                <div>
                  <n-text strong>{{ row.label }}</n-text>
                  <n-text depth="3" style="display:block;font-size:12px;">{{ row.status }}</n-text>
                </div>
                <n-space align="center" :wrap="false">
                  <n-text>{{ row.count }}（{{ row.percentage_text }}）</n-text>
                  <n-button
                    v-if="row.sample_available"
                    size="small"
                    tertiary
                    @click="openPreviewSamples(row)"
                  >
                    查看样本
                  </n-button>
                </n-space>
              </n-space>
            </n-list-item>
          </n-list>
        </template>
        <template v-if="safeCleanupJob?.state === 'preview_ready'">
          <n-divider>显式确认</n-divider>
          <n-text depth="3">输入“确认删除已核验孤儿人物”后才允许开始。</n-text>
          <n-input v-model:value="safeCleanupConfirmation" style="margin-top: 8px;" />
        </template>
        <n-space justify="end" style="margin-top: 16px;">
          <n-button
            v-if="['previewing', 'running', 'stop_requested'].includes(safeCleanupJob?.state)"
            type="warning"
            @click="stopSafeCleanup"
          >
            安全停止
          </n-button>
          <n-button
            v-if="safeCleanupJob?.state === 'preview_ready'"
            type="error"
            :disabled="safeCleanupConfirmation !== '确认删除已核验孤儿人物' || safeCleanupConfirming"
            :loading="safeCleanupConfirming"
            @click="confirmSafeCleanup"
          >
            确认串行删除
          </n-button>
        </n-space>
      </n-card>
    </n-modal>

    <n-modal v-model:show="previewSamplesVisible" :mask-closable="!previewSamplesLoading">
      <n-card
        class="person-verify-card"
        :title="`预览样本：${previewSamplesLabel}`"
        closable
        @close="previewSamplesVisible = false"
      >
        <n-alert type="info" style="margin-bottom: 12px;">
          样本来自该 cleanup job 的持久化 job_items；不会实时访问 Emby 或重新核验人物。
        </n-alert>
        <n-data-table
          :columns="previewSampleColumns"
          :data="previewSamplesItems"
          :loading="previewSamplesLoading"
          :row-key="row => row.person_id"
          :pagination="false"
          :scroll-x="900"
        />
        <n-empty
          v-if="!previewSamplesLoading && previewSamplesItems.length === 0"
          description="该分类没有持久化样本"
          size="small"
          style="margin-top: 12px;"
        />
        <n-space justify="end" style="margin-top: 16px;">
          <n-pagination
            v-if="previewSamplesTotal > previewSamplesPageSize"
            :page="previewSamplesPage"
            :page-count="previewSamplesPageCount"
            :page-size="previewSamplesPageSize"
            @update:page="fetchPreviewSamples"
          />
        </n-space>
      </n-card>
    </n-modal>
  </n-layout>
</template>

<script setup>
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import axios from 'axios';
import {
  buildPersonCleanupPreviewRows,
  personCleanupPreviewLabel,
} from '../utils/personCleanupPreview';
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
  NInput,
  NLayout,
  NList,
  NListItem,
  NModal,
  NPageHeader,
  NPagination,
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
const protectionSnapshot = ref({ state: 'unknown', generation: null });
const snapshotGeneration = ref(null);
const readonlyScan = ref(null);
const safeCleanupModalVisible = ref(false);
const safeCleanupJob = ref(null);
const safeCleanupConfirmation = ref('');
const safeCleanupConfirming = ref(false);
const previewSamplesVisible = ref(false);
const previewSamplesLoading = ref(false);
const previewSamplesState = ref('');
const previewSamplesLabel = ref('');
const previewSamplesItems = ref([]);
const previewSamplesPage = ref(1);
const previewSamplesPageSize = 5;
const previewSamplesTotal = ref(0);
let safeCleanupPollTimer = null;
const pagination = { pageSize: 30, showSizePicker: true, pageSizes: [20, 30, 50, 100] };

const currentAction = computed(() => props.taskStatus?.current_action || '');
const isBackgroundBusy = computed(() => Boolean(props.taskStatus?.is_running));
const isScanRunning = computed(() => isBackgroundBusy.value && currentAction.value.includes('扫描幽灵人物'));
const isDeleteRunning = computed(() => isBackgroundBusy.value && currentAction.value.includes('删除') && currentAction.value.includes('幽灵人物'));
const previewStateRows = computed(() => (
  buildPersonCleanupPreviewRows(safeCleanupJob.value?.preview_summary)
));
const previewSamplesPageCount = computed(() => Math.max(
  1,
  Math.ceil(previewSamplesTotal.value / previewSamplesPageSize),
));
const readonlyScanTitle = computed(() => {
  const state = readonlyScan.value?.state;
  if (state === 'completed') return '两阶段只读扫描已完成';
  if (state === 'stopped') return '保护别名核验已中止，可继续';
  if (state === 'interrupted') return '保护别名核验在重启后等待继续';
  return '阶段 2：核验保护库别名人物';
});
const verificationAlertType = computed(() => ({
  orphan: 'success',
  identity_alias_only: 'info',
  protected_library_alias: 'info',
  protected_library_unverifiable: 'warning',
  linked: 'warning',
  people_unavailable: 'warning',
  connection_failed: 'error',
  invalid_response: 'error',
}[verificationResult.value?.status] || 'error'));
const verificationAlertTitle = computed(() => ({
  orphan: '当前精确关联作品为 0',
  identity_alias_only: '仅发现其他 Person 的关联作品',
  protected_library_alias: '受保护媒体库别名人物',
  protected_library_unverifiable: '受保护媒体库人物明细不可核验',
  linked: `发现 ${verificationResult.value?.reference_count || 0} 部精确关联作品`,
  people_unavailable: '作品人物明细不可核验',
  connection_failed: '无法连接 Emby',
  invalid_response: 'Emby 响应异常',
}[verificationResult.value?.status] || '核对失败'));
const verificationSummary = computed(() => {
  const result = verificationResult.value;
  if (!result) return '-';
  if (result.status === 'people_unavailable') {
    return `${result.query_reference_count || 0} 部可能关联作品，People 不可核验`;
  }
  if (result.status === 'protected_library_alias') {
    return '该人物仅以其他 Person 身份关联受保护媒体库作品，已移出待复核';
  }
  if (result.status === 'protected_library_unverifiable') {
    return '受保护媒体库作品的 People 明细无法完整核验，已按保护处理并移出待复核';
  }
  if (['connection_failed', 'invalid_response'].includes(result.status)) {
    return '核对未完成，禁止删除';
  }
  if (result.status === 'identity_alias_only') {
    return `${result.query_reference_count || 0} 部查询命中，当前 Person ID 精确关联为 0`;
  }
  return `${result.reference_count || 0} 部当前精确关联作品`;
});

const imageUrl = (personId) => `/image_proxy/Items/${personId}/Images/Primary?maxWidth=160&quality=85`;
const isVerifiedOrphan = (row) => Boolean(
  row.verification_status === 'orphan'
  && row.verification_snapshot_generation === snapshotGeneration.value
  && !row.last_error,
);
const formatDate = (value) => {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
};

const columns = [
  {
    type: 'selection',
    multiple: true,
    disabled: (row) => !isVerifiedOrphan(row),
  },
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
      if (row.verification_status === 'identity_alias_only') {
        return h(NTag, { type: 'warning', bordered: false }, () => '同身份别名：受保护');
      }
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

const previewSampleColumns = [
  {
    title: '人物',
    key: 'person_name',
    minWidth: 170,
    render: (row) => h('div', null, [
      h('strong', null, row.person_name || '未知人物'),
      h(NText, { depth: 3, style: 'display:block;font-size:12px;' }, () => `Emby: ${row.person_id}`),
    ]),
  },
  {
    title: '外部 ID',
    key: 'provider_ids_json',
    minWidth: 170,
    render: (row) => providerIdText(row.provider_ids_json),
  },
  {
    title: '预览状态',
    key: 'preview_state',
    minWidth: 190,
    render: (row) => h('div', null, [
      h(NText, null, () => personCleanupPreviewLabel(row.preview_state)),
      h(NText, { depth: 3, style: 'display:block;font-size:12px;' }, () => row.preview_state),
    ]),
  },
  {
    title: '原因',
    key: 'last_error',
    minWidth: 260,
    render: (row) => row.last_error || '无持久化错误信息',
  },
];

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
      if (response.data.status === 'identity_alias_only') {
        message.warning(response.data.message || '仅命中同身份的其他 Person，该人物保持受保护且不可删除');
      } else {
        message.success(response.data.message || '核对完成，可以人工勾选');
      }
    }
  } catch (error) {
    const result = error.response?.data;
    if (result?.status) {
      verificationResult.value = result;
      if (result.candidate) {
        const index = candidates.value.findIndex((item) => item.person_id === row.person_id);
        if (index >= 0) candidates.value[index] = result.candidate;
      }
      message.warning(result.message || result.error || '核对未完成，该人物禁止删除');
    } else {
      verifyError.value = result?.error || '无法完成人物关联核对';
    }
  } finally {
    verifyLoading.value = false;
  }
};

const fetchCandidates = async () => {
  loading.value = true;
  try {
    const response = await axios.get('/api/person-cleanup/candidates');
    candidates.value = response.data.candidates || [];
    readonlyScan.value = response.data.readonly_scan || null;
    snapshotGeneration.value = response.data.snapshot_generation;
    protectionSnapshot.value = {
      state: response.data.snapshot_state || 'ready',
      generation: response.data.snapshot_generation,
    };
    const validIds = new Set(
      candidates.value
        .filter(isVerifiedOrphan)
        .map((item) => item.person_id),
    );
    selectedIds.value = selectedIds.value.filter((personId) => validIds.has(personId));
    loadError.value = '';
  } catch (error) {
    loadError.value = error.response?.data?.error || '无法读取人物候选';
    const snapshot = error.response?.data?.snapshot_state;
    readonlyScan.value = error.response?.data?.readonly_scan || readonlyScan.value;
    if (snapshot && typeof snapshot === 'object') {
      protectionSnapshot.value = {
        ...snapshot,
        state: snapshot.snapshot_state || snapshot.state || 'unknown',
      };
    }
  } finally {
    loading.value = false;
  }
};

const fetchProtectedLibraries = async () => {
  protectedLoading.value = true;
  try {
    const response = await axios.get('/api/person-cleanup/protected-libraries');
    protectedLibraries.value = response.data.libraries || [];
    const snapshot = response.data.snapshot || {};
    protectionSnapshot.value = {
      ...snapshot,
      state: snapshot.snapshot_state || snapshot.state || 'unknown',
    };
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
    await fetchCandidates();
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

const fetchLatestSafeCleanupJob = async () => {
  try {
    const response = await axios.get('/api/person-cleanup/cleanup-jobs/latest');
    if (response.data.job) safeCleanupJob.value = response.data.job;
  } catch (error) {
    message.error(error.response?.data?.error || '无法读取最近一次安全清理预览');
  }
};

const openLatestSafeCleanupJob = () => {
  safeCleanupConfirmation.value = '';
  safeCleanupModalVisible.value = true;
};

const fetchPreviewSamples = async (page = 1) => {
  if (!safeCleanupJob.value?.job_id || !previewSamplesState.value) return;
  previewSamplesLoading.value = true;
  try {
    const response = await axios.get(
      `/api/person-cleanup/cleanup-jobs/${encodeURIComponent(safeCleanupJob.value.job_id)}/preview-items`,
      {
        params: {
          state: previewSamplesState.value,
          page,
          page_size: previewSamplesPageSize,
        },
      },
    );
    previewSamplesItems.value = response.data.items || [];
    previewSamplesPage.value = response.data.page || page;
    previewSamplesTotal.value = response.data.total || 0;
  } catch (error) {
    previewSamplesItems.value = [];
    previewSamplesTotal.value = 0;
    message.error(error.response?.data?.error || '无法读取持久化预览样本');
  } finally {
    previewSamplesLoading.value = false;
  }
};

const openPreviewSamples = async (row) => {
  if (!row?.sample_available) return;
  previewSamplesState.value = row.status;
  previewSamplesLabel.value = row.label;
  previewSamplesPage.value = 1;
  previewSamplesItems.value = [];
  previewSamplesTotal.value = 0;
  previewSamplesVisible.value = true;
  await fetchPreviewSamples(1);
};

const scheduleSafeCleanupPoll = () => {
  if (safeCleanupPollTimer) window.clearTimeout(safeCleanupPollTimer);
  const terminalStates = new Set(['preview_ready', 'completed', 'stopped', 'failed', 'superseded', 'interrupted_requires_repreview']);
  if (!safeCleanupJob.value || terminalStates.has(safeCleanupJob.value.state)) return;
  safeCleanupPollTimer = window.setTimeout(pollSafeCleanupJob, 1000);
};

const pollSafeCleanupJob = async () => {
  if (!safeCleanupJob.value?.job_id) return;
  try {
    const response = await axios.get(`/api/person-cleanup/cleanup-jobs/${encodeURIComponent(safeCleanupJob.value.job_id)}`);
    safeCleanupJob.value = response.data.job;
    scheduleSafeCleanupPoll();
    if (['completed', 'stopped'].includes(safeCleanupJob.value.state)) await fetchCandidates();
  } catch (error) {
    message.error(error.response?.data?.error || '无法读取安全清理任务状态');
  }
};

const startSafeCleanupPreview = async () => {
  try {
    const response = await axios.post('/api/person-cleanup/cleanup-jobs/preview');
    safeCleanupJob.value = { job_id: response.data.job_id, state: response.data.state };
    safeCleanupConfirmation.value = '';
    safeCleanupModalVisible.value = true;
    scheduleSafeCleanupPoll();
  } catch (error) {
    message.error(error.response?.data?.error || '无法创建安全清理预览');
  }
};

const confirmSafeCleanup = async () => {
  safeCleanupConfirming.value = true;
  try {
    const jobId = safeCleanupJob.value.job_id;
    const tokenResponse = await axios.post(`/api/person-cleanup/cleanup-jobs/${encodeURIComponent(jobId)}/confirmation-token`);
    await axios.post(`/api/person-cleanup/cleanup-jobs/${encodeURIComponent(jobId)}/confirm`, {
      confirmation: safeCleanupConfirmation.value,
      confirmation_token: tokenResponse.data.confirmation_token,
    });
    safeCleanupJob.value = { ...safeCleanupJob.value, state: 'confirmed' };
    scheduleSafeCleanupPoll();
  } catch (error) {
    message.error(error.response?.data?.error || '安全清理确认失败');
  } finally {
    safeCleanupConfirming.value = false;
  }
};

const stopSafeCleanup = async () => {
  try {
    await axios.post(`/api/person-cleanup/cleanup-jobs/${encodeURIComponent(safeCleanupJob.value.job_id)}/stop`);
    safeCleanupJob.value = { ...safeCleanupJob.value, state: 'stop_requested' };
    scheduleSafeCleanupPoll();
  } catch (error) {
    message.error(error.response?.data?.error || '无法停止安全清理任务');
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
  fetchLatestSafeCleanupJob();
});

onBeforeUnmount(() => {
  if (safeCleanupPollTimer) window.clearTimeout(safeCleanupPollTimer);
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

.person-verify-card {
  width: min(760px, calc(100vw - 32px));
  max-height: min(82vh, calc(100dvh - 48px));
  overflow: auto;
}

@media (max-width: 767px) {
  .person-cleanup-page :deep(.n-layout-scroll-container) {
    padding: 14px 12px 96px !important;
  }

  .person-cleanup-header :deep(.n-page-header__main) {
    align-items: flex-start;
    gap: 12px;
  }

  .person-cleanup-header :deep(.n-page-header__extra) {
    width: 100%;
  }

  .person-cleanup-actions {
    width: 100%;
  }

  .person-cleanup-actions :deep(.n-button) {
    flex: 1 1 30%;
  }

  .protected-libraries-panel :deep(.n-space) {
    max-width: 100%;
  }

  .person-verify-card {
    width: calc(100vw - 20px);
    max-height: calc(100dvh - 20px);
  }
}
</style>
