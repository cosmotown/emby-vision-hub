<template>
  <n-modal v-model:show="visible" preset="card" title="选择容器目录" style="width: min(680px, 92vw);">
    <n-space vertical :size="14">
      <n-input-group>
        <n-input v-model:value="currentPath" @keyup.enter="loadDirectory(currentPath)" />
        <n-button @click="loadDirectory(currentPath)" :loading="loading">前往</n-button>
      </n-input-group>

      <n-space justify="space-between" align="center">
        <n-button :disabled="!parentPath" @click="loadDirectory(parentPath)">上一级</n-button>
        <n-text depth="3">仅显示 Toolkit 容器内可访问的目录</n-text>
      </n-space>

      <n-alert v-if="errorMessage" type="error" :show-icon="true">{{ errorMessage }}</n-alert>
      <n-spin :show="loading">
        <div class="directory-list">
          <n-empty v-if="!loading && directories.length === 0" description="当前目录没有可浏览的子目录" />
          <button
            v-for="directory in directories"
            :key="directory.path"
            type="button"
            class="directory-row"
            @dblclick="loadDirectory(directory.path)"
          >
            <span class="directory-name" @click="loadDirectory(directory.path)">{{ directory.name }}</span>
            <n-button size="small" quaternary @click.stop="loadDirectory(directory.path)">进入</n-button>
          </button>
        </div>
      </n-spin>
    </n-space>
    <template #footer>
      <n-space justify="end">
        <n-button @click="visible = false">取消</n-button>
        <n-button type="primary" @click="chooseCurrent">选择当前目录</n-button>
      </n-space>
    </template>
  </n-modal>
</template>

<script setup>
import { computed, ref, watch } from 'vue';
import axios from 'axios';
import {
  NAlert,
  NButton,
  NEmpty,
  NInput,
  NInputGroup,
  NModal,
  NSpace,
  NSpin,
  NText,
  useMessage,
} from 'naive-ui';

const props = defineProps({
  show: { type: Boolean, default: false },
  initialPath: { type: String, default: '/' },
});
const emit = defineEmits(['update:show', 'select']);
const message = useMessage();
const currentPath = ref('/');
const parentPath = ref(null);
const directories = ref([]);
const loading = ref(false);
const errorMessage = ref('');

const visible = computed({
  get: () => props.show,
  set: (value) => emit('update:show', value),
});

const loadDirectory = async (path) => {
  if (!path) return;
  loading.value = true;
  try {
    const response = await axios.get('/api/directories', { params: { path } });
    currentPath.value = response.data.path || path;
    parentPath.value = response.data.parent || null;
    directories.value = response.data.directories || [];
    errorMessage.value = '';
  } catch (error) {
    errorMessage.value = error.response?.data?.error || '无法读取目录';
  } finally {
    loading.value = false;
  }
};

const chooseCurrent = () => {
  emit('select', currentPath.value);
  visible.value = false;
  message.success(`已选择 ${currentPath.value}`);
};

watch(
  () => props.show,
  (show) => {
    if (show) loadDirectory(props.initialPath || '/');
  },
);
</script>

<style scoped>
.directory-list {
  min-height: 240px;
  max-height: 420px;
  overflow-y: auto;
  border: 1px solid var(--n-border-color);
  border-radius: 6px;
}
.directory-row {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 12px;
  color: var(--n-text-color);
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--n-border-color);
  cursor: pointer;
  text-align: left;
}
.directory-row:last-child { border-bottom: 0; }
.directory-row:hover { background: var(--n-color-hover); }
.directory-name {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
