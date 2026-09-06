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
            type="info"
            secondary
            :loading="isAliasProofRunning"
            :disabled="isBackgroundBusy && !isAliasProofRunning"
            @click="startAliasProof"
          >
            Alias Orphan 只读证明
          </n-button>
          <n-button
            type="info"
            secondary
            :loading="isStaleIndexRunning"
            :disabled="isBackgroundBusy && !isStaleIndexRunning"
            @click="startStaleIndex"
          >
            Stale Index 只读取证
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
            secondary
            @click="openSafeCleanupHistory"
          >
            历史安全任务
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

    <n-card v-if="aliasProof" size="small" title="Alias Orphan 只读证明" style="margin-bottom: 16px;">
      <n-alert type="info" style="margin-bottom: 12px;">
        verified_alias_orphan 仅表示满足当前只读安全证明；本版本不会删除人物，也不会改变现有删除资格。
      </n-alert>
      <n-descriptions bordered :column="2" label-placement="left">
        <n-descriptions-item label="状态">{{ aliasProof.state }}</n-descriptions-item>
        <n-descriptions-item label="进度">
          {{ aliasProof.checked_count || 0 }} / {{ aliasProof.candidate_total || 0 }}
        </n-descriptions-item>
        <n-descriptions-item label="verified alias orphan">
          {{ aliasProof.verified_alias_orphan_count || 0 }}
        </n-descriptions-item>
        <n-descriptions-item label="受保护 / 拒绝 / 失败">
          {{ aliasProof.protected_count || 0 }} / {{ aliasProof.rejected_count || 0 }} / {{ aliasProof.failed_count || 0 }}
        </n-descriptions-item>
      </n-descriptions>
      <n-list v-if="aliasProofStates.length" bordered style="margin-top: 12px;">
        <n-list-item v-for="row in aliasProofStates" :key="row.proof_state">
          <n-space justify="space-between" align="center">
            <span>{{ aliasProofStateLabel(row.proof_state) }}（{{ row.proof_state }}）</span>
            <n-space align="center">
              <n-text>{{ row.count }}</n-text>
              <n-button
                v-if="!['pending', 'checking'].includes(row.proof_state) && row.count"
                size="small"
                tertiary
                @click="openAliasProofSamples(row.proof_state)"
              >查看样本</n-button>
            </n-space>
          </n-space>
        </n-list-item>
      </n-list>
      <n-alert v-if="aliasProof.last_error" type="warning" style="margin-top: 12px;">
        {{ aliasProof.last_error }}
      </n-alert>
      <n-space justify="end" style="margin-top: 12px;">
        <n-button
          v-if="['running', 'stop_requested'].includes(aliasProof.state)"
          type="warning"
          @click="stopAliasProof"
        >安全停止</n-button>
        <n-button
          v-if="['stopped', 'interrupted'].includes(aliasProof.state)"
          type="primary"
          secondary
          :disabled="isBackgroundBusy"
          @click="startAliasProof"
        >继续只读证明</n-button>
      </n-space>
    </n-card>

    <n-card v-if="staleIndexRun" size="small" title="Stale Index 只读取证" style="margin-bottom: 16px;">
      <n-alert type="warning" style="margin-bottom: 12px;">
        该结果仅证明当前 PersonIds 索引与实际 People 关系不一致，不代表本版本允许删除人物。
        verified_stale_index_signature 与 stable_stale_index_signature 均为只读证据状态。
      </n-alert>
      <n-descriptions bordered :column="2" label-placement="left">
        <n-descriptions-item label="状态">{{ staleIndexRun.state }}</n-descriptions-item>
        <n-descriptions-item label="取证 generation">
          {{ staleIndexRun.forensic_generation || '-' }}
        </n-descriptions-item>
        <n-descriptions-item label="source proof">
          identity_not_found / {{ staleIndexRun.source_proof_id || '-' }}
        </n-descriptions-item>
        <n-descriptions-item label="进度">
          {{ staleIndexRun.checked_count || 0 }} / {{ staleIndexRun.candidate_total || 0 }}
        </n-descriptions-item>
        <n-descriptions-item label="取证 signature">
          {{ staleIndexRun.verified_signature_count || 0 }}
        </n-descriptions-item>
        <n-descriptions-item label="两次独立稳定证明">
          {{ staleIndexRun.stable_signature_count || 0 }}
        </n-descriptions-item>
      </n-descriptions>
      <n-list v-if="staleIndexRows.length" bordered style="margin-top: 12px;">
        <n-list-item v-for="row in staleIndexRows" :key="`${row.dimension}:${row.state}`">
          <n-space justify="space-between" align="center">
            <span>{{ staleIndexStateLabel(row.state) }}（{{ row.state }}）</span>
            <n-space align="center">
              <n-text>{{ row.count }}</n-text>
              <n-button
                v-if="!['pending', 'checking'].includes(row.state) && row.count"
                size="small"
                tertiary
                @click="openStaleIndexSamples(row)"
              >查看样本</n-button>
            </n-space>
          </n-space>
        </n-list-item>
      </n-list>
      <n-card
        v-if="staleIndexRun.state === 'stale'"
        size="small"
        title="漂移诊断"
        style="margin-top: 12px;"
      >
        <n-alert type="error" style="margin-bottom: 12px;">
          这些变化导致本轮证据失败关闭；未降低任何安全条件，所有 signature 与 stable pass 均已清零。
        </n-alert>
        <template v-if="staleIndexRun.final_snapshot_generation !== null && staleIndexRun.final_snapshot_generation !== undefined">
          <n-descriptions bordered :column="2" label-placement="left">
          <n-descriptions-item label="Protection">
            {{ driftLabel(staleIndexRun.drift_protection || staleIndexRun.drift_generation) }}
          </n-descriptions-item>
          <n-descriptions-item label="Normal People relationship">
            {{ driftLabel(staleIndexRun.drift_normal_relationship) }}
          </n-descriptions-item>
          <n-descriptions-item label="Person identity">
            {{ driftLabel(staleIndexRun.drift_person) }}
          </n-descriptions-item>
          <n-descriptions-item label="Source Alias Proof">
            {{ driftLabel(staleIndexRun.drift_source_proof) }}
          </n-descriptions-item>
          <n-descriptions-item label="Protection 变化明细" :span="2">
            {{ diagnosticSummaryUnavailable(staleIndexRun.protection_drift_summary)
              || protectionDriftText(staleIndexRun.protection_drift_summary) }}
          </n-descriptions-item>
          <n-descriptions-item
            v-if="diagnosticSummaryUnavailable(staleIndexRun.normal_relationship_drift_summary)"
            label="关系变化摘要"
            :span="2"
          >
            {{ diagnosticSummaryUnavailable(staleIndexRun.normal_relationship_drift_summary) }}
          </n-descriptions-item>
          <n-descriptions-item
            v-if="diagnosticSummaryUnavailable(staleIndexRun.person_drift_summary)"
            label="Person 变化摘要"
            :span="2"
          >
            {{ diagnosticSummaryUnavailable(staleIndexRun.person_drift_summary) }}
          </n-descriptions-item>
          <n-descriptions-item
            v-if="staleIndexRun.source_proof_drift_summary?.reason"
            label="Source proof 原因"
            :span="2"
          >
            {{ staleIndexRun.source_proof_drift_summary.reason }}
          </n-descriptions-item>
          </n-descriptions>

          <n-descriptions bordered :column="2" label-placement="left" style="margin-top: 12px;">
          <n-descriptions-item
            v-if="staleIndexRun.normal_relationship_drift_summary?.available !== false"
            label="媒体数量（起始 / 最终）"
          >
            {{ staleIndexRun.normal_relationship_drift_summary?.start_media_count || 0 }} /
            {{ staleIndexRun.normal_relationship_drift_summary?.final_media_count || 0 }}
          </n-descriptions-item>
          <n-descriptions-item
            v-if="staleIndexRun.normal_relationship_drift_summary?.available !== false"
            label="媒体新增 / 删除"
          >
            {{ staleIndexRun.normal_relationship_drift_summary?.added_item_count || 0 }} /
            {{ staleIndexRun.normal_relationship_drift_summary?.removed_item_count || 0 }}
          </n-descriptions-item>
          <n-descriptions-item
            v-if="staleIndexRun.normal_relationship_drift_summary?.available !== false"
            label="People / Type / 库归属变化"
          >
            {{ staleIndexRun.normal_relationship_drift_summary?.changed_item_people_count || 0 }} /
            {{ staleIndexRun.normal_relationship_drift_summary?.changed_item_type_count || 0 }} /
            {{ staleIndexRun.normal_relationship_drift_summary?.changed_library_ownership_count || 0 }}
          </n-descriptions-item>
          <n-descriptions-item
            v-if="staleIndexRun.normal_relationship_drift_summary?.available !== false"
            label="People 新增 / 移除 / 改名"
          >
            {{ staleIndexRun.normal_relationship_drift_summary?.people_added_count || 0 }} /
            {{ staleIndexRun.normal_relationship_drift_summary?.people_removed_count || 0 }} /
            {{ staleIndexRun.normal_relationship_drift_summary?.people_name_changed_count || 0 }}
          </n-descriptions-item>
          <n-descriptions-item
            v-if="staleIndexRun.person_drift_summary?.available !== false"
            label="Person 新增 / 移除"
          >
            {{ staleIndexRun.person_drift_summary?.person_added_count || 0 }} /
            {{ staleIndexRun.person_drift_summary?.person_removed_count || 0 }}
          </n-descriptions-item>
          <n-descriptions-item
            v-if="staleIndexRun.person_drift_summary?.available !== false"
            label="Person Name / ProviderIds 变化"
          >
            {{ staleIndexRun.person_drift_summary?.person_name_changed_count || 0 }} /
            {{ staleIndexRun.person_drift_summary?.person_provider_ids_changed_count || 0 }}
          </n-descriptions-item>
          </n-descriptions>

          <n-collapse style="margin-top: 12px;">
          <n-collapse-item
            v-if="staleIndexRun.normal_relationship_drift_summary?.samples?.length"
            title="关系变化样本（最多 20 条）"
            name="relationship-drift-samples"
          >
            <n-list bordered>
              <n-list-item
                v-for="sample in staleIndexRun.normal_relationship_drift_summary.samples"
                :key="`relationship:${sample.item_id}:${sample.change_type}`"
              >
                Item {{ sample.item_id }} · {{ sample.change_type }} · People
                {{ sample.start_people_count }} → {{ sample.final_people_count }}
              </n-list-item>
            </n-list>
          </n-collapse-item>
          <n-collapse-item
            v-if="staleIndexRun.person_drift_summary?.samples?.length"
            title="Person 变化样本（最多 20 条）"
            name="person-drift-samples"
          >
            <n-list bordered>
              <n-list-item
                v-for="sample in staleIndexRun.person_drift_summary.samples"
                :key="`person:${sample.person_id}:${sample.change_type}`"
              >
                Person {{ sample.person_id }} · {{ sample.change_type }}
                <span v-if="sample.old_provider_identities?.length || sample.new_provider_identities?.length">
                  · identity {{ (sample.old_provider_identities || []).join(', ') || '无' }}
                  → {{ (sample.new_provider_identities || []).join(', ') || '无' }}
                </span>
              </n-list-item>
            </n-list>
          </n-collapse-item>
          </n-collapse>
        </template>
        <n-alert v-else type="warning">
          该历史记录生成于漂移诊断功能之前，无法判断具体漂移来源；不会据此推断任何 snapshot 未变化。
        </n-alert>
      </n-card>
      <n-alert v-if="staleIndexRun.last_error" type="warning" style="margin-top: 12px;">
        {{ staleIndexRun.last_error }}
      </n-alert>
      <n-space justify="end" style="margin-top: 12px;">
        <n-button
          v-if="['running', 'stop_requested'].includes(staleIndexRun.state)"
          type="warning"
          @click="stopStaleIndex"
        >安全停止</n-button>
        <n-button
          v-if="['stopped', 'interrupted'].includes(staleIndexRun.state)"
          type="primary"
          secondary
          :disabled="isBackgroundBusy"
          @click="startStaleIndex"
        >继续只读取证</n-button>
      </n-space>
    </n-card>

    <n-card size="small" title="Stable Stale Index Safe Delete Canary" style="margin-bottom: 16px;">
      <n-alert type="error" style="margin-bottom: 12px;">
        独立 Canary 仅从连续两轮 completed Stale Index 稳定证据中确定性抽样，后端硬上限 100。
        这是不可逆 Person 删除操作，不会批量删除全部稳定候选。任一抽样人物预检拒绝则整次预览不可执行。
        它不是 orphan 清理，也不提供全量删除、全选或 limit 覆盖；停止或重启后该任务不可 resume。
        预览仅只读；明确确认后，本 execution 最多一次管理员登录。认证或会话失效即停止，不自动重新登录。
      </n-alert>
      <n-space v-if="!staleDeleteCanaryJob" justify="end">
        <n-button type="warning" :disabled="isBackgroundBusy" @click="startStaleDeleteCanaryPreview">
          创建最多 100 人 Canary 预览
        </n-button>
      </n-space>
      <template v-else>
        <n-descriptions bordered :column="2" label-placement="left">
          <n-descriptions-item label="状态">{{ staleDeleteCanaryJob.state }}</n-descriptions-item>
          <n-descriptions-item label="硬上限">100（不可覆盖）</n-descriptions-item>
          <n-descriptions-item label="管理员认证">
            {{ staleDeleteCanaryJob.admin_auth_state || 'pending' }}（{{ staleDeleteCanaryJob.admin_auth_attempts || 0 }} / 1）
          </n-descriptions-item>
          <n-descriptions-item label="管理员会话已核验">
            {{ staleDeleteCanaryJob.admin_session_verified === true ? '是' : '否' }}
          </n-descriptions-item>
          <n-descriptions-item label="稳定证据 generation">
            {{ staleDeleteCanaryJob.previous_generation }} → {{ staleDeleteCanaryJob.latest_generation }}
          </n-descriptions-item>
          <n-descriptions-item label="符合条件 / 抽样">
            {{ staleDeleteCanaryJob.eligible_total || 0 }} / {{ staleDeleteCanaryJob.candidate_total || 0 }}
          </n-descriptions-item>
          <n-descriptions-item label="Canary ready">{{ staleDeleteCanaryJob.ready_count || 0 }}</n-descriptions-item>
          <n-descriptions-item label="确认删除">{{ staleDeleteCanaryJob.confirmed_deleted_count || 0 }}</n-descriptions-item>
          <n-descriptions-item label="Stable 总数">{{ staleDeleteCanaryJob.stable_total || 0 }}</n-descriptions-item>
          <n-descriptions-item label="Same-name 排除">{{ staleDeleteCanaryJob.same_name_excluded || 0 }}</n-descriptions-item>
          <n-descriptions-item label="预检拒绝">{{ staleDeleteCanaryJob.items?.filter(item => item.preview_state === 'preflight_rejected').length || 0 }}</n-descriptions-item>
          <n-descriptions-item label="结果不确定">{{ staleDeleteCanaryJob.ambiguous_count || 0 }}</n-descriptions-item>
        </n-descriptions>
        <n-alert v-if="staleDeleteCanaryJob.last_error" type="warning" style="margin-top: 12px;">
          {{ staleDeleteCanaryJob.last_error }}
        </n-alert>
        <n-alert v-if="staleDeleteCanaryJob.final_verification?.person_delta_exact === true" type="success" style="margin-top: 12px;">
          全局终检通过：保护和 People 关系未变化；Person 集合仅减去已逐项回读确认删除的 ID。
        </n-alert>
        <n-list v-if="staleDeleteCanaryJob.items?.length" bordered style="margin-top: 12px; max-height: 280px; overflow: auto;">
          <n-list-item v-for="item in staleDeleteCanaryJob.items" :key="item.person_id">
            <n-space justify="space-between">
              <span>{{ item.person_name || item.person_id }}（{{ item.person_id }}）</span>
              <n-text>{{ item.preview_state }} / {{ item.execute_state }}</n-text>
            </n-space>
            <n-text v-if="item.last_error" depth="3">{{ item.last_error }}</n-text>
            <n-text v-if="item.post_attempts" depth="3">提交次数 {{ item.post_attempts }}；HTTP {{ item.http_status ?? '未知' }}；回读 {{ item.readback_state || '未确认' }}</n-text>
          </n-list-item>
        </n-list>
        <n-space justify="end" style="margin-top: 12px;">
          <n-button
            v-if="['previewing', 'preview_ready', 'confirmed', 'preflighting', 'running', 'stop_requested'].includes(staleDeleteCanaryJob.state)"
            type="warning"
            @click="stopStaleDeleteCanary"
          >在人物边界停止</n-button>
          <n-button
            v-if="staleDeleteCanaryJob.state === 'preview_ready'"
            type="error"
            :disabled="isBackgroundBusy"
            @click="openStaleDeleteCanaryConfirmation"
          >确认 Canary 串行删除</n-button>
          <n-button
            v-if="!['previewing', 'preview_ready', 'confirmed', 'preflighting', 'running', 'stop_requested'].includes(staleDeleteCanaryJob.state)"
            type="warning"
            secondary
            :disabled="isBackgroundBusy"
            @click="startStaleDeleteCanaryPreview"
          >创建新的 Canary 预览</n-button>
        </n-space>
      </template>
    </n-card>

    <n-modal v-model:show="staleDeleteCanaryConfirmVisible" :mask-closable="false">
      <n-card class="person-verify-card" title="确认 Stable Stale Index Canary" closable @close="staleDeleteCanaryConfirmVisible = false">
        <n-alert type="error" style="margin-bottom: 12px;">
          令牌仅对当前固定预览有效并在 10 分钟后过期。执行串行且遇到任何失败立即停止，不会自动重放。
        </n-alert>
        <n-text>请输入：确认删除稳定陈旧索引 Canary 人物</n-text>
        <n-input v-model:value="staleDeleteCanaryConfirmation" style="margin-top: 8px;" />
        <n-space justify="end" style="margin-top: 12px;">
          <n-button @click="staleDeleteCanaryConfirmVisible = false">取消</n-button>
          <n-button
            type="error"
            :loading="staleDeleteCanaryConfirming"
            :disabled="staleDeleteCanaryConfirmation !== '确认删除稳定陈旧索引 Canary 人物'"
            @click="confirmStaleDeleteCanary"
          >执行独立 Canary</n-button>
        </n-space>
      </n-card>
    </n-modal>

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

    <n-modal v-model:show="safeCleanupHistoryVisible" :mask-closable="!safeCleanupHistoryLoading">
      <n-card
        class="person-cleanup-history-card"
        title="历史安全任务"
        closable
        @close="safeCleanupHistoryVisible = false"
      >
        <n-alert type="info" style="margin-bottom: 12px;">
          历史任务及分类统计只读取已持久化的 cleanup job/job_items；不会重新访问 Emby、重新核验或修改任务。
        </n-alert>
        <n-data-table
          :columns="safeCleanupHistoryColumns"
          :data="safeCleanupHistoryJobs"
          :loading="safeCleanupHistoryLoading"
          :row-key="row => row.job_id"
          :pagination="false"
          :scroll-x="1050"
        />
        <n-empty
          v-if="!safeCleanupHistoryLoading && safeCleanupHistoryJobs.length === 0"
          description="没有持久化安全清理任务"
          size="small"
          style="margin-top: 12px;"
        />
      </n-card>
    </n-modal>

    <n-modal v-model:show="safeCleanupModalVisible" :mask-closable="false">
      <n-card class="person-verify-card" :title="safeCleanupModalTitle" closable @close="safeCleanupModalVisible = false">
        <n-alert
          :type="safeCleanupViewMode === 'history' ? 'info' : 'warning'"
          :title="safeCleanupViewMode === 'history' ? '历史任务只读详情' : '持久化安全任务'"
          style="margin-bottom: 16px;"
        >
          <template v-if="safeCleanupViewMode === 'history'">
            这里只读取该任务已持久化的 preview 和 job_items；不会停止、确认或重新执行历史任务。
          </template>
          <template v-else>
            预览不会删除人物。确认后将串行执行；每位人物删除前都会刷新完整保护快照并实时核验，且删除尝试必须先持久化后才发送一次 POST。
          </template>
        </n-alert>
        <n-descriptions v-if="safeCleanupJob" bordered :column="1" label-placement="left">
          <n-descriptions-item label="状态">{{ safeCleanupJob.state }}</n-descriptions-item>
          <n-descriptions-item label="候选">{{ safeCleanupJob.candidate_total || 0 }}</n-descriptions-item>
          <n-descriptions-item label="显式 orphan">{{ safeCleanupJob.verified_orphan_count || 0 }}</n-descriptions-item>
          <n-descriptions-item label="受保护/跳过">{{ safeCleanupJob.protected_count || 0 }} / {{ safeCleanupJob.skipped_count || 0 }}</n-descriptions-item>
          <n-descriptions-item label="核验失败/删除失败">{{ safeCleanupJob.verification_failed_count || 0 }} / {{ safeCleanupJob.failed_count || 0 }}</n-descriptions-item>
          <n-descriptions-item v-if="safeCleanupJob.preview_summary" label="预览进度">
            {{ safeCleanupJob.preview_summary.preview_progress_count || 0 }} /
            {{ safeCleanupJob.preview_summary.preview_expected_count || 0 }}
            （{{ safeCleanupJob.preview_summary.preview_complete ? '已完成' : '未完成' }}）
          </n-descriptions-item>
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
        <template v-if="safeCleanupViewMode !== 'history' && safeCleanupJob?.state === 'preview_ready'">
          <n-divider>显式确认</n-divider>
          <n-text depth="3">输入“确认删除已核验孤儿人物”后才允许开始。</n-text>
          <n-input v-model:value="safeCleanupConfirmation" style="margin-top: 8px;" />
        </template>
        <n-space justify="end" style="margin-top: 16px;">
          <n-button
            v-if="safeCleanupViewMode !== 'history' && ['previewing', 'running', 'stop_requested'].includes(safeCleanupJob?.state)"
            type="warning"
            @click="stopSafeCleanup"
          >
            安全停止
          </n-button>
          <n-button
            v-if="safeCleanupViewMode !== 'history' && safeCleanupJob?.state === 'preview_ready'"
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

    <n-modal v-model:show="aliasProofSamplesVisible" :mask-closable="!aliasProofSamplesLoading">
      <n-card class="person-verify-card" :title="`只读证明样本：${aliasProofStateLabel(aliasProofSampleState)}`" closable @close="aliasProofSamplesVisible = false">
        <n-alert type="info" style="margin-bottom: 12px;">
          样本只读取 PostgreSQL 中已持久化的 proof items，不会访问 Emby 或执行删除。
        </n-alert>
        <n-data-table
          :columns="aliasProofSampleColumns"
          :data="aliasProofSamples"
          :loading="aliasProofSamplesLoading"
          :row-key="row => row.person_id"
          :pagination="false"
          :scroll-x="1050"
        />
        <n-pagination
          v-if="aliasProofSamplesTotal > aliasProofSamplesPageSize"
          :page="aliasProofSamplesPage"
          :page-count="aliasProofSamplesPageCount"
          :page-size="aliasProofSamplesPageSize"
          style="margin-top: 12px;justify-content:flex-end;"
          @update:page="fetchAliasProofSamples"
        />
      </n-card>
    </n-modal>

    <n-modal v-model:show="staleIndexSamplesVisible" :mask-closable="!staleIndexSamplesLoading">
      <n-card class="person-verify-card" :title="`Stale Index 样本：${staleIndexStateLabel(staleIndexSampleState)}`" closable @close="staleIndexSamplesVisible = false">
        <n-data-table
          :columns="staleIndexSampleColumns"
          :data="staleIndexSamples"
          :loading="staleIndexSamplesLoading"
          :pagination="false"
          :scroll-x="1100"
        />
        <n-pagination
          v-if="staleIndexSamplesTotal > staleIndexSamplesPageSize"
          :page="staleIndexSamplesPage"
          :page-count="staleIndexSamplesPageCount"
          :page-size="staleIndexSamplesPageSize"
          style="margin-top: 12px; justify-content: flex-end;"
          @update:page="fetchStaleIndexSamples"
        />
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
const safeCleanupViewMode = ref('active');
const safeCleanupConfirmation = ref('');
const safeCleanupConfirming = ref(false);
const safeCleanupHistoryVisible = ref(false);
const safeCleanupHistoryLoading = ref(false);
const safeCleanupHistoryJobs = ref([]);
const previewSamplesVisible = ref(false);
const previewSamplesLoading = ref(false);
const previewSamplesState = ref('');
const previewSamplesLabel = ref('');
const previewSamplesItems = ref([]);
const previewSamplesPage = ref(1);
const previewSamplesPageSize = 5;
const previewSamplesTotal = ref(0);
const aliasProof = ref(null);
const aliasProofSamplesVisible = ref(false);
const aliasProofSamplesLoading = ref(false);
const aliasProofSampleState = ref('');
const aliasProofSamples = ref([]);
const aliasProofSamplesPage = ref(1);
const aliasProofSamplesPageSize = 20;
const aliasProofSamplesTotal = ref(0);
const staleIndexRun = ref(null);
const staleIndexSamplesVisible = ref(false);
const staleIndexSamplesLoading = ref(false);
const staleIndexSampleState = ref('');
const staleIndexSampleDimension = ref('forensic_state');
const staleIndexSamples = ref([]);
const staleIndexSamplesPage = ref(1);
const staleIndexSamplesPageSize = 20;
const staleIndexSamplesTotal = ref(0);
const staleDeleteCanaryJob = ref(null);
const staleDeleteCanaryConfirmVisible = ref(false);
const staleDeleteCanaryConfirmation = ref('');
const staleDeleteCanaryToken = ref('');
const staleDeleteCanaryConfirming = ref(false);
let safeCleanupPollTimer = null;
let aliasProofPollTimer = null;
let staleIndexPollTimer = null;
let staleDeleteCanaryPollTimer = null;
const pagination = { pageSize: 30, showSizePicker: true, pageSizes: [20, 30, 50, 100] };

const currentAction = computed(() => props.taskStatus?.current_action || '');
const isBackgroundBusy = computed(() => Boolean(props.taskStatus?.is_running));
const isScanRunning = computed(() => isBackgroundBusy.value && currentAction.value.includes('扫描幽灵人物'));
const isDeleteRunning = computed(() => isBackgroundBusy.value && currentAction.value.includes('删除') && currentAction.value.includes('幽灵人物'));
const isAliasProofRunning = computed(() => isBackgroundBusy.value && currentAction.value.includes('Alias Orphan'));
const isStaleIndexRunning = computed(() => isBackgroundBusy.value && currentAction.value.includes('Stale Index'));
const aliasProofStates = computed(() => aliasProof.value?.states || []);
const aliasProofSamplesPageCount = computed(() => Math.max(
  1,
  Math.ceil(aliasProofSamplesTotal.value / aliasProofSamplesPageSize),
));
const staleIndexRows = computed(() => [
  ...(staleIndexRun.value?.states || []).map((row) => ({
    state: row.forensic_state, count: row.count, dimension: 'forensic_state',
  })),
  ...(staleIndexRun.value?.identity_signals || []).map((row) => ({
    state: row.signal, count: row.count, dimension: 'identity_signal',
  })),
  ...(staleIndexRun.value?.people_signals || []).map((row) => ({
    state: row.signal, count: row.count, dimension: 'people_signal',
  })),
]);
const staleIndexSamplesPageCount = computed(() => Math.max(
  1,
  Math.ceil(staleIndexSamplesTotal.value / staleIndexSamplesPageSize),
));
const previewStateRows = computed(() => (
  buildPersonCleanupPreviewRows(safeCleanupJob.value?.preview_summary)
));
const safeCleanupModalTitle = computed(() => (
  safeCleanupViewMode.value === 'history' ? '历史安全任务详情' : '一键安全清理'
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

const aliasProofStateLabel = (state) => ({
  pending: '等待核验',
  checking: '正在核验',
  verified_alias_orphan: '已通过只读 alias orphan 证明',
  linked: '当前仍有关联',
  protected: '命中保护合同',
  identity_unavailable: '外部身份不足',
  identity_not_found: '未找到在用同身份人物',
  identity_ambiguous: '同身份人物不唯一',
  people_unavailable: 'People 无法完整核验',
  invalid_response: 'Emby 响应异常',
  connection_failed: '连接失败',
  candidate_changed: '候选身份已变化',
  failed_safe: '失败关闭',
}[state] || '其他状态');

const aliasProofSampleColumns = [
  { title: 'Person ID', key: 'person_id', minWidth: 130 },
  { title: '人物', key: 'person_name', minWidth: 150 },
  {
    title: '外部 ID', key: 'candidate_provider_ids', minWidth: 180,
    render: (row) => providerIdText(row.candidate_provider_ids),
  },
  { title: 'proof state', key: 'proof_state', minWidth: 180 },
  { title: '在用同身份 Person', key: 'matched_live_person_id', minWidth: 150, render: (row) => row.matched_live_person_id || '-' },
  { title: '查询 / 精确关联', key: 'counts', minWidth: 130, render: (row) => `${row.query_count || 0} / ${row.exact_reference_count || 0}` },
  { title: '原因', key: 'error', minWidth: 250, render: (row) => row.error || '-' },
];

const staleIndexStateLabel = (state) => ({
  pending: '等待取证',
  checking: '正在取证',
  verified_stale_index_signature: '单次陈旧索引 signature',
  stable_stale_index_signature: '两次独立稳定 signature',
  query_disappeared: 'PersonIds 查询已自行消失',
  linked: '当前 actual People 已重新关联',
  protected: '命中保护合同',
  people_unavailable: 'People 无法完整核验',
  candidate_changed: 'candidate fingerprint 已变化',
  person_missing: '当前 Person 已不存在',
  identity_owner_live: '当前出现同身份在用 Person',
  failed_safe: '失败关闭',
  stale_index_no_identity_owner: '没有其他 canonical identity owner',
  stale_index_identity_owner_not_live: '同身份 owner 也未被实际引用',
  stale_index_same_name_other_person: '查询作品实际引用同名其他 Person',
  stale_index_different_people: '查询作品实际引用不同人物',
  stale_index_no_actual_people: '查询作品实际 People 为空',
}[state] || '其他状态');

const driftLabel = (changed) => (changed ? '发生变化' : '未变化');

const diagnosticSummaryUnavailable = (summary = {}) => (
  summary?.available === false
    ? `诊断摘要不可用（${summary?.error || 'unknown'}）；原始 snapshot drift 仍按失败关闭处理`
    : ''
);

const protectionDriftText = (summary = {}) => {
  const labels = [
    ['generation_changed', 'generation'],
    ['protected_ids_changed', 'protected IDs'],
    ['protected_names_changed', 'protected names'],
    ['protected_provider_identities_changed', 'protected provider identities'],
    ['persistent_aliases_changed', 'persistent aliases'],
    ['selected_protected_libraries_changed', 'selected protected libraries'],
    ['root_contract_changed', 'root contract'],
  ].filter(([key]) => summary?.[key]).map(([, label]) => label);
  return labels.length ? labels.join('、') : '未检测到 protection component 变化';
};

const staleIndexSampleColumns = [
  { title: 'Person ID', key: 'person_id', minWidth: 130 },
  { title: '人物', key: 'person_name', minWidth: 150 },
  {
    title: '外部 ID', key: 'provider_ids', minWidth: 180,
    render: (row) => providerIdText(row.provider_ids),
  },
  { title: '取证状态', key: 'forensic_state', minWidth: 190 },
  { title: '身份信号', key: 'identity_signal', minWidth: 190, render: (row) => row.identity_signal || '-' },
  { title: 'People 信号', key: 'people_signal', minWidth: 210, render: (row) => row.people_signal || '-' },
  { title: '查询项', key: 'query_count', width: 90 },
  { title: '实际 People', key: 'actual_people_count', width: 110 },
  { title: '稳定次数', key: 'stable_pass_count', width: 100 },
  { title: '原因', key: 'error', minWidth: 260, render: (row) => row.error || '-' },
];

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

const safeCleanupHistoryColumns = [
  {
    title: '时间',
    key: 'created_at',
    width: 180,
    render: (row) => formatDate(row.created_at),
  },
  { title: '状态', key: 'state', minWidth: 150 },
  { title: '候选数', key: 'candidate_total', width: 90 },
  { title: '显式 orphan', key: 'verified_orphan_count', width: 110 },
  { title: '核验失败', key: 'verification_failed_count', width: 100 },
  { title: '已删除', key: 'deleted_count', width: 90 },
  { title: '删除失败', key: 'failed_count', width: 100 },
  {
    title: '操作',
    key: 'actions',
    width: 90,
    fixed: 'right',
    render: (row) => h(NButton, {
      size: 'small',
      secondary: true,
      onClick: () => openHistoricalSafeCleanupJob(row),
    }, () => '查看'),
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

const fetchAliasProof = async () => {
  try {
    const response = await axios.get('/api/person-cleanup/alias-proof-runs/latest');
    aliasProof.value = response.data.proof || null;
    scheduleAliasProofPoll();
  } catch (error) {
    message.error(error.response?.data?.error || '无法读取 Alias Orphan 只读证明状态');
  }
};

const scheduleAliasProofPoll = () => {
  if (aliasProofPollTimer) window.clearTimeout(aliasProofPollTimer);
  if (!aliasProof.value || !['running', 'stop_requested'].includes(aliasProof.value.state)) return;
  aliasProofPollTimer = window.setTimeout(fetchAliasProof, 1200);
};

const startAliasProof = async () => {
  try {
    const resumable = ['stopped', 'interrupted'].includes(aliasProof.value?.state);
    await axios.post('/api/person-cleanup/alias-proof-runs', {
      proof_id: resumable ? aliasProof.value.proof_id : null,
    });
    message.success('Alias Orphan 只读证明已提交；本版本不会删除人物');
    window.setTimeout(fetchAliasProof, 500);
  } catch (error) {
    message.error(error.response?.data?.error || '无法启动 Alias Orphan 只读证明');
  }
};

const stopAliasProof = async () => {
  if (!aliasProof.value?.proof_id) return;
  try {
    await axios.post(`/api/person-cleanup/alias-proof-runs/${encodeURIComponent(aliasProof.value.proof_id)}/stop`);
    aliasProof.value = { ...aliasProof.value, state: 'stop_requested' };
    scheduleAliasProofPoll();
  } catch (error) {
    message.error(error.response?.data?.error || '无法停止只读证明');
  }
};

const fetchAliasProofSamples = async (page = 1) => {
  if (!aliasProof.value?.proof_id || !aliasProofSampleState.value) return;
  aliasProofSamplesLoading.value = true;
  try {
    const response = await axios.get(
      `/api/person-cleanup/alias-proof-runs/${encodeURIComponent(aliasProof.value.proof_id)}/items`,
      { params: { state: aliasProofSampleState.value, page, page_size: aliasProofSamplesPageSize } },
    );
    aliasProofSamples.value = response.data.items || [];
    aliasProofSamplesPage.value = response.data.page || page;
    aliasProofSamplesTotal.value = response.data.total || 0;
  } catch (error) {
    message.error(error.response?.data?.error || '无法读取只读证明样本');
  } finally {
    aliasProofSamplesLoading.value = false;
  }
};

const openAliasProofSamples = async (state) => {
  aliasProofSampleState.value = state;
  aliasProofSamplesPage.value = 1;
  aliasProofSamplesVisible.value = true;
  await fetchAliasProofSamples(1);
};

const fetchStaleIndex = async () => {
  try {
    const response = await axios.get('/api/person-cleanup/stale-index-runs/latest');
    staleIndexRun.value = response.data.run || null;
    scheduleStaleIndexPoll();
  } catch (error) {
    message.error(error.response?.data?.error || '无法读取 Stale Index 只读取证状态');
  }
};

const scheduleStaleIndexPoll = () => {
  if (staleIndexPollTimer) window.clearTimeout(staleIndexPollTimer);
  if (!staleIndexRun.value || !['running', 'stop_requested'].includes(staleIndexRun.value.state)) return;
  staleIndexPollTimer = window.setTimeout(fetchStaleIndex, 1200);
};

const startStaleIndex = async () => {
  try {
    const resumable = ['stopped', 'interrupted'].includes(staleIndexRun.value?.state);
    await axios.post('/api/person-cleanup/stale-index-runs', {
      run_id: resumable ? staleIndexRun.value.run_id : null,
    });
    message.success('Stale Index 只读取证已提交；不会修改或删除人物');
    window.setTimeout(fetchStaleIndex, 500);
  } catch (error) {
    message.error(error.response?.data?.error || '无法启动 Stale Index 只读取证');
  }
};

const stopStaleIndex = async () => {
  if (!staleIndexRun.value?.run_id) return;
  try {
    await axios.post(`/api/person-cleanup/stale-index-runs/${encodeURIComponent(staleIndexRun.value.run_id)}/stop`);
    staleIndexRun.value = { ...staleIndexRun.value, state: 'stop_requested' };
    scheduleStaleIndexPoll();
  } catch (error) {
    message.error(error.response?.data?.error || '无法停止 Stale Index 只读取证');
  }
};

const fetchStaleIndexSamples = async (page = 1) => {
  if (!staleIndexRun.value?.run_id || !staleIndexSampleState.value) return;
  staleIndexSamplesLoading.value = true;
  try {
    const response = await axios.get(
      `/api/person-cleanup/stale-index-runs/${encodeURIComponent(staleIndexRun.value.run_id)}/items`,
      {
        params: {
          state: staleIndexSampleState.value,
          dimension: staleIndexSampleDimension.value,
          page,
          page_size: staleIndexSamplesPageSize,
        },
      },
    );
    staleIndexSamples.value = response.data.items || [];
    staleIndexSamplesPage.value = response.data.page || page;
    staleIndexSamplesTotal.value = response.data.total || 0;
  } catch (error) {
    message.error(error.response?.data?.error || '无法读取 Stale Index 持久化样本');
  } finally {
    staleIndexSamplesLoading.value = false;
  }
};

const openStaleIndexSamples = async (row) => {
  staleIndexSampleState.value = row.state;
  staleIndexSampleDimension.value = row.dimension;
  staleIndexSamplesPage.value = 1;
  staleIndexSamplesVisible.value = true;
  await fetchStaleIndexSamples(1);
};

const scheduleStaleDeleteCanaryPoll = () => {
  if (staleDeleteCanaryPollTimer) window.clearTimeout(staleDeleteCanaryPollTimer);
  if (!staleDeleteCanaryJob.value || !['previewing', 'confirmed', 'preflighting', 'running', 'stop_requested'].includes(staleDeleteCanaryJob.value.state)) return;
  staleDeleteCanaryPollTimer = window.setTimeout(fetchStaleDeleteCanary, 1200);
};

const fetchStaleDeleteCanary = async () => {
  try {
    const response = await axios.get('/api/person-cleanup/stale-delete-canary/latest');
    staleDeleteCanaryJob.value = response.data.job || null;
    scheduleStaleDeleteCanaryPoll();
  } catch (error) {
    message.error(error.response?.data?.error || '无法读取 Stale Index Canary 状态');
  }
};

const startStaleDeleteCanaryPreview = async () => {
  try {
    await axios.post('/api/person-cleanup/stale-delete-canary/preview', { limit: 100 });
    message.success('Canary GET-only 预览已提交；尚未执行删除');
    window.setTimeout(fetchStaleDeleteCanary, 500);
  } catch (error) {
    message.error(error.response?.data?.error || '无法创建 Stale Index Canary 预览');
  }
};

const openStaleDeleteCanaryConfirmation = async () => {
  try {
    const response = await axios.post(
      `/api/person-cleanup/stale-delete-canary/${encodeURIComponent(staleDeleteCanaryJob.value.job_id)}/confirmation-token`,
    );
    staleDeleteCanaryToken.value = response.data.confirmation_token;
    staleDeleteCanaryConfirmation.value = '';
    staleDeleteCanaryConfirmVisible.value = true;
  } catch (error) {
    message.error(error.response?.data?.error || '无法签发 Canary 短时确认令牌');
  }
};

const confirmStaleDeleteCanary = async () => {
  staleDeleteCanaryConfirming.value = true;
  try {
    await axios.post(
      `/api/person-cleanup/stale-delete-canary/${encodeURIComponent(staleDeleteCanaryJob.value.job_id)}/confirm`,
      {
        confirmation: staleDeleteCanaryConfirmation.value,
        confirmation_token: staleDeleteCanaryToken.value,
      },
    );
    staleDeleteCanaryConfirmVisible.value = false;
    staleDeleteCanaryToken.value = '';
    message.warning('Stale Index Canary 已确认，开始串行执行并逐项严格回读');
    window.setTimeout(fetchStaleDeleteCanary, 500);
  } catch (error) {
    message.error(error.response?.data?.error || 'Stale Index Canary 确认失败');
  } finally {
    staleDeleteCanaryConfirming.value = false;
  }
};

const stopStaleDeleteCanary = async () => {
  try {
    await axios.post(
      `/api/person-cleanup/stale-delete-canary/${encodeURIComponent(staleDeleteCanaryJob.value.job_id)}/stop`,
    );
    staleDeleteCanaryJob.value = { ...staleDeleteCanaryJob.value, state: 'stop_requested' };
    scheduleStaleDeleteCanaryPoll();
  } catch (error) {
    message.error(error.response?.data?.error || '无法停止 Stale Index Canary');
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

const openSafeCleanupHistory = async () => {
  safeCleanupHistoryVisible.value = true;
  safeCleanupHistoryLoading.value = true;
  try {
    const response = await axios.get('/api/person-cleanup/cleanup-jobs', {
      params: { limit: 20 },
    });
    safeCleanupHistoryJobs.value = response.data.jobs || [];
  } catch (error) {
    safeCleanupHistoryJobs.value = [];
    message.error(error.response?.data?.error || '无法读取历史安全任务');
  } finally {
    safeCleanupHistoryLoading.value = false;
  }
};

const openHistoricalSafeCleanupJob = async (row) => {
  if (!row?.job_id) return;
  try {
    const response = await axios.get(
      `/api/person-cleanup/cleanup-jobs/${encodeURIComponent(row.job_id)}`,
    );
    safeCleanupJob.value = response.data.job;
    safeCleanupViewMode.value = 'history';
    safeCleanupHistoryVisible.value = false;
    previewSamplesVisible.value = false;
    safeCleanupConfirmation.value = '';
    safeCleanupModalVisible.value = true;
    scheduleSafeCleanupPoll();
  } catch (error) {
    message.error(error.response?.data?.error || '无法读取历史安全任务详情');
  }
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
    safeCleanupViewMode.value = 'active';
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
  fetchAliasProof();
  fetchStaleIndex();
  fetchStaleDeleteCanary();
});

onBeforeUnmount(() => {
  if (safeCleanupPollTimer) window.clearTimeout(safeCleanupPollTimer);
  if (aliasProofPollTimer) window.clearTimeout(aliasProofPollTimer);
  if (staleIndexPollTimer) window.clearTimeout(staleIndexPollTimer);
  if (staleDeleteCanaryPollTimer) window.clearTimeout(staleDeleteCanaryPollTimer);
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

.person-cleanup-history-card {
  width: min(1120px, calc(100vw - 32px));
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

  .person-cleanup-history-card {
    width: calc(100vw - 20px);
    max-height: calc(100dvh - 20px);
  }
}
</style>
