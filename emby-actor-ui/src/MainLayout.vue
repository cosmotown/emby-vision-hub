<template>
  <n-layout class="app-shell">
    <div class="app-backdrop" aria-hidden="true" />
    <n-layout-header class="app-header" :bordered="false">
      <div class="header-brand">
        <img :src="logo" alt="Emby Vision Hub" class="brand-logo" />
        <div class="brand-copy">
          <strong>EMBY VISION HUB</strong>
          <span>COSMOTOWN EDITION</span>
        </div>
      </div>

      <div
        v-if="!isMobile && authStore.isAdmin && hasActiveTask"
        class="header-task-status"
      >
        <n-spin v-if="props.taskStatus?.is_running" size="small" />
        <n-icon v-else :component="TimerOutline" size="18" />
        <div class="task-copy">
          <strong>{{ props.taskStatus.current_action }}</strong>
          <span>{{ props.taskStatus.message }}</span>
        </div>
        <n-progress
          v-if="props.taskStatus?.is_running && props.taskStatus.progress >= 0"
          type="line"
          :percentage="props.taskStatus.progress"
          :show-indicator="false"
          processing
          class="task-progress"
        />
        <n-button
          v-if="props.taskStatus?.is_running"
          type="error"
          size="tiny"
          circle
          secondary
          @click="triggerStopTask"
        >
          <template #icon><n-icon :component="Stop" /></template>
        </n-button>
      </div>

      <div class="header-actions">
        <n-tooltip v-if="!isMobile && authStore.isAdmin">
          <template #trigger>
            <n-button quaternary circle aria-label="查看实时日志" @click="isRealtimeLogVisible = true">
              <template #icon><n-icon :component="ReaderOutline" /></template>
            </n-button>
          </template>
          实时日志
        </n-tooltip>
        <n-tooltip v-if="!isMobile && authStore.isAdmin">
          <template #trigger>
            <n-button quaternary circle aria-label="查看历史日志" @click="isHistoryLogVisible = true">
              <template #icon><n-icon :component="ArchiveOutline" /></template>
            </n-button>
          </template>
          历史日志
        </n-tooltip>
        <n-dropdown
          v-if="authStore.isLoggedIn"
          trigger="click"
          placement="bottom-end"
          :options="userOptions"
          :menu-props="userMenuProps"
          @select="handleUserSelect"
        >
          <button type="button" class="user-menu-button" aria-label="打开用户菜单">
            <span class="user-avatar">{{ usernameInitial }}</span>
            <span v-if="!isMobile" class="user-menu-copy">
              <strong>{{ authStore.username }}</strong>
              <small>{{ authStore.isAdmin ? '管理员' : '用户' }}</small>
            </span>
          </button>
        </n-dropdown>
      </div>
    </n-layout-header>

    <div v-if="isHorizontal" class="horizontal-navigation">
      <n-menu
        mode="horizontal"
        responsive
        :options="horizontalMenuOptions"
        :value="activeMenuKey"
        @update:value="handleMenuUpdate"
      />
    </div>

    <n-layout
      has-sider
      class="app-body"
      :class="{ 'with-horizontal-navigation': isHorizontal }"
    >
      <n-layout-sider
        v-if="!isMobile && !isHorizontal"
        :bordered="false"
        collapse-mode="width"
        :collapsed-width="72"
        :width="248"
        :collapsed="isCollapsedLayout"
        show-trigger="bar"
        :native-scrollbar="false"
        class="app-sider"
        @update:collapsed="handleDesktopCollapse"
      >
        <div class="sider-version" :class="{ compact: isCollapsedLayout }">
          <span v-if="!isCollapsedLayout">功能导航</span>
          <small>v{{ appVersion }}</small>
        </div>
        <n-menu
          :collapsed="isCollapsedLayout"
          :collapsed-width="72"
          :collapsed-icon-size="22"
          :options="verticalMenuOptions"
          :value="activeMenuKey"
          @update:value="handleMenuUpdate"
        />
      </n-layout-sider>

      <n-layout-content class="app-main-content-wrapper" :native-scrollbar="false">
        <main class="page-content-inner-wrapper">
          <router-view v-slot="slotProps">
            <component :is="slotProps.Component" :task-status="props.taskStatus" />
          </router-view>
        </main>
      </n-layout-content>
    </n-layout>

    <nav v-if="isMobile" class="mobile-bottom-navigation" aria-label="主要导航">
      <button
        v-for="item in mobileMenuOptions"
        :key="item.key"
        type="button"
        :class="{ active: activeMenuKey === item.key }"
        @click="handleMenuUpdate(item.key)"
      >
        <n-icon :component="item.icon" size="20" />
        <span>{{ item.label }}</span>
      </button>
      <button
        type="button"
        :class="{ active: activeMenuKey === 'MobileMore' }"
        @click="handleMenuUpdate('MobileMore')"
      >
        <n-icon :component="GridOutline" size="20" />
        <span>更多</span>
      </button>
    </nav>

    <n-modal
      v-model:show="isRealtimeLogVisible"
      preset="card"
      class="modal-card-lite"
      style="width: 95%; max-width: 900px"
      title="实时任务日志"
    >
      <n-log ref="logRef" :log="logContent" trim class="log-panel" />
    </n-modal>

    <LogViewer v-model:show="isHistoryLogVisible" />
    <ThemeCustomizer
      v-model:show="showThemeCustomizer"
      :model-value="props.themeSettings"
      :mobile="isMobile"
      @update:model-value="emit('update:theme-settings', $event)"
      @reset="emit('reset-theme-settings')"
    />
  </n-layout>
</template>

<script setup>
import { computed, h, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  NButton,
  NDropdown,
  NIcon,
  NLayout,
  NLayoutContent,
  NLayoutHeader,
  NLayoutSider,
  NLog,
  NMenu,
  NModal,
  NProgress,
  NSpin,
  NTooltip,
  useDialog,
  useMessage,
} from 'naive-ui';
import {
  ArchiveOutline,
  BookOutline,
  ColorPaletteOutline,
  GridOutline,
  InformationCircleOutline,
  LogOutOutline,
  OptionsOutline,
  PeopleCircleOutline,
  PersonCircleOutline,
  ReaderOutline,
  RefreshOutline,
  Stop,
  TimerOutline,
} from '@vicons/ionicons5';
import axios from 'axios';
import { useAuthStore } from './stores/auth';
import {
  getVisibleMobileRoutes,
  getVisibleNavigation,
  getVisibleTopNavigation,
} from './navigation.js';
import LogViewer from './components/LogViewer.vue';
import ThemeCustomizer from './components/ThemeCustomizer.vue';
import logo from './assets/evh-logo.svg';

const props = defineProps({
  themeSettings: {
    type: Object,
    required: true,
  },
  taskStatus: Object,
});

const emit = defineEmits(['update:theme-settings', 'reset-theme-settings']);
const router = useRouter();
const route = useRoute();
const authStore = useAuthStore();
const message = useMessage();
const dialog = useDialog();

const appVersion = ref(__APP_VERSION__);
const isMobile = ref(false);
const isRealtimeLogVisible = ref(false);
const isHistoryLogVisible = ref(false);
const showThemeCustomizer = ref(false);
const logRef = ref(null);

const activeMenuKey = computed(() => route.name);
const isHorizontal = computed(() => !isMobile.value && props.themeSettings.layout === 'horizontal');
const isCollapsedLayout = computed(() => props.themeSettings.layout === 'collapsed');
const hasActiveTask = computed(() => {
  const action = props.taskStatus?.current_action;
  return action && !['空闲', '无'].includes(action);
});
const usernameInitial = computed(() => (authStore.username || 'U').slice(0, 1).toUpperCase());
const logContent = computed(() => props.taskStatus?.logs?.join('\n') || '等待任务日志...');
const visibleNavigation = computed(() => getVisibleNavigation(authStore));
const visibleTopNavigation = computed(() => getVisibleTopNavigation(authStore));
const mobileMenuOptions = computed(() => getVisibleMobileRoutes(authStore));

const renderIcon = (icon) => () => h(NIcon, null, { default: () => h(icon) });
const themeLabelMap = {
  auto: '跟随系统',
  light: '浅色',
  dark: '深色',
  purple: '幻紫',
  transparent: '透明',
};
const layoutLabelMap = {
  vertical: '垂直',
  collapsed: '折叠',
  horizontal: '水平',
};
const userMenuProps = () => ({ class: 'user-profile-dropdown' });

const renderUserMenuHeader = () => h('div', { class: 'user-dropdown-profile' }, [
  h('span', { class: 'user-dropdown-profile-avatar' }, usernameInitial.value),
  h('div', { class: 'user-dropdown-profile-copy' }, [
    h('small', authStore.isAdmin ? '管理员' : '用户'),
    h('strong', authStore.username || 'User'),
  ]),
]);

const verticalMenuOptions = computed(() => [
  ...visibleTopNavigation.value.map((item) => ({
    label: item.label,
    key: item.key,
    icon: renderIcon(item.icon),
  })),
  ...visibleNavigation.value.map((section) => ({
    type: 'group',
    label: section.label,
    key: section.key,
    children: section.items.map((item) => ({
      label: item.label,
      key: item.key,
      icon: renderIcon(item.icon),
    })),
  })),
]);

const horizontalMenuOptions = computed(() => [
  ...visibleTopNavigation.value.map((item) => ({
    label: item.label,
    key: item.key,
    icon: renderIcon(item.icon),
  })),
  ...visibleNavigation.value.map((section) => ({
    label: section.label,
    key: section.key,
    children: section.items.map((item) => ({
      label: item.label,
      key: item.key,
      icon: renderIcon(item.icon),
    })),
  })),
]);

const userOptions = computed(() => {
  const options = [
    { type: 'render', key: 'profile-header', render: renderUserMenuHeader },
    { type: 'divider', key: 'profile-divider' },
    { label: '个人中心', key: 'user-center', icon: renderIcon(PersonCircleOutline) },
  ];
  if (authStore.isAdmin) {
    options.push(
      { label: '系统设置', key: 'settings-general', icon: renderIcon(OptionsOutline) },
      { label: '用户管理', key: 'user-management', icon: renderIcon(PeopleCircleOutline) },
    );
  }
  options.push(
    {
      label: '主题与外观',
      key: 'theme-customizer',
      icon: renderIcon(ColorPaletteOutline),
      extra: `${themeLabelMap[props.themeSettings.theme] || '跟随系统'} · ${layoutLabelMap[props.themeSettings.layout] || '垂直'}`,
    },
  );
  if (authStore.isAdmin) {
    options.push({ label: '关于 EVH', key: 'releases', icon: renderIcon(InformationCircleOutline) });
  }
  options.push(
    { label: '帮助文档', key: 'help-docs', icon: renderIcon(BookOutline) },
    { type: 'divider', key: 'actions-divider' },
  );
  if (authStore.isAdmin) {
    options.push({ label: '重启容器', key: 'restart-container', icon: renderIcon(RefreshOutline) });
  }
  options.push({
    label: '退出登录',
    key: 'logout',
    icon: renderIcon(LogOutOutline),
    props: { class: 'user-dropdown-logout-option' },
  });
  return options;
});

function checkMobile() {
  isMobile.value = window.innerWidth < 768;
}

function openThemeCustomizer() {
  showThemeCustomizer.value = true;
}

function handleMenuUpdate(key) {
  if (!key || String(key).startsWith('section-')) return;
  router.push({ name: key });
}

function handleDesktopCollapse(collapsed) {
  if (isMobile.value) return;
  emit('update:theme-settings', {
    ...props.themeSettings,
    layout: collapsed ? 'collapsed' : 'vertical',
  });
}

function applyLayout(layout) {
  emit('update:theme-settings', { ...props.themeSettings, layout });
}

function applyTheme(theme) {
  emit('update:theme-settings', { ...props.themeSettings, theme });
}

async function triggerStopTask() {
  try {
    await axios.post('/api/trigger_stop_task');
    message.info('已发送停止任务请求。');
  } catch (error) {
    message.error(error.response?.data?.error || '发送停止任务请求失败，请查看日志。');
  }
}

async function triggerRestart() {
  message.info('正在发送重启指令...');
  try {
    await axios.post('/api/system/restart');
    message.success('重启指令已发送，请稍后刷新页面。', { duration: 10000 });
  } catch (error) {
    if (error.response) {
      message.error(error.response.data.error || '发送重启请求失败，请查看日志。');
      return;
    }
    message.success('重启指令已发送，请稍后刷新页面。', { duration: 10000 });
  }
}

async function handleUserSelect(key) {
  if (key === 'user-center') {
    router.push({ name: 'UserCenter' });
  } else if (key === 'settings-general') {
    router.push({ name: 'settings-general' });
  } else if (key === 'user-management') {
    router.push({ name: 'UserManagement' });
  } else if (key === 'releases') {
    router.push({ name: 'Releases' });
  } else if (String(key).startsWith('layout:')) {
    applyLayout(String(key).split(':')[1]);
  } else if (String(key).startsWith('theme:')) {
    applyTheme(String(key).split(':')[1]);
  } else if (key === 'theme-customizer') {
    showThemeCustomizer.value = true;
  } else if (key === 'restart-container') {
    dialog.warning({
      title: '确认重启容器',
      content: '确定要重启容器吗？应用将在短时间内无法访问。',
      positiveText: '确定重启',
      negativeText: '取消',
      onPositiveClick: triggerRestart,
    });
  } else if (key === 'help-docs') {
    window.open('https://github.com/cosmotown/emby-vision-hub', '_blank');
  } else if (key === 'logout') {
    await authStore.logout();
    router.push({ name: 'Login' });
  }
}

watch([() => props.taskStatus?.logs, isRealtimeLogVisible], async ([, visible]) => {
  if (!visible) return;
  await nextTick();
  logRef.value?.scrollTo({ position: 'bottom', silent: true });
}, { deep: true });

onMounted(() => {
  checkMobile();
  window.addEventListener('resize', checkMobile);
  window.addEventListener('evh:open-theme-customizer', openThemeCustomizer);
});

onUnmounted(() => {
  window.removeEventListener('resize', checkMobile);
  window.removeEventListener('evh:open-theme-customizer', openThemeCustomizer);
});
</script>

<style scoped>
.app-shell {
  position: relative;
  height: 100vh;
  color: var(--app-text);
  background: var(--app-background);
}

.app-backdrop {
  position: fixed;
  z-index: 0;
  inset: 0;
  pointer-events: none;
  background-color: rgba(128, 128, 128, 0.30);
  background-image:
    linear-gradient(rgba(0, 0, 0, 0.30), rgba(0, 0, 0, 0.60)),
    linear-gradient(rgba(128, 128, 128, 0.30), rgba(128, 128, 128, 0.30)),
    var(--app-backdrop-image, none);
  background-position: center;
  background-repeat: no-repeat;
  background-size: cover;
  filter: blur(var(--transparent-background-blur, 16px));
  opacity: var(--transparent-background-poster-opacity, 1);
  transform: scale(var(--app-backdrop-scale, 1.03));
}

.app-header {
  position: relative;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 68px;
  padding: 0 20px;
  border-bottom: 1px solid var(--app-border-subtle);
  background: var(--app-surface);
}

.header-brand,
.header-actions,
.user-menu-button,
.header-task-status {
  display: flex;
  align-items: center;
}

.header-brand { gap: 10px; }
.header-actions { gap: 4px; }
.brand-logo { width: 34px; height: 34px; object-fit: contain; }
.brand-copy { display: flex; flex-direction: column; line-height: 1.05; letter-spacing: 0.08em; }
.brand-copy strong { font-size: 14px; }
.brand-copy span { margin-top: 4px; color: var(--app-text-muted); font-size: 9px; }
.mobile-menu-button { font-size: 22px; }

.header-task-status {
  max-width: min(46vw, 620px);
  gap: 10px;
  padding: 7px 12px;
  border: 1px solid var(--app-border-subtle);
  border-radius: 999px;
  background: var(--app-surface-soft);
}

.task-copy { display: flex; min-width: 0; flex-direction: column; }
.task-copy strong,
.task-copy span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-copy strong { font-size: 12px; }
.task-copy span { color: var(--app-text-muted); font-size: 11px; }
.task-progress { width: 88px; }

.user-menu-button {
  gap: 8px;
  padding: 4px 6px;
  border: 0;
  color: inherit;
  background: transparent;
  cursor: pointer;
}

.user-avatar {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border-radius: 50%;
  color: #fff;
  background: var(--app-primary);
  font-size: 13px;
  font-weight: 700;
}

.user-menu-copy { display: flex; flex-direction: column; min-width: 70px; text-align: left; }
.user-menu-copy strong { font-size: 12px; }
.user-menu-copy small { color: var(--app-text-muted); font-size: 10px; }

.horizontal-navigation {
  position: relative;
  z-index: 20;
  height: 50px;
  padding: 0 24px;
  border-bottom: 1px solid var(--app-border-subtle);
  background: var(--app-surface);
}

.app-header,
.horizontal-navigation,
.app-body,
.mobile-bottom-navigation {
  position: relative;
}

.app-body { z-index: 1; height: calc(100vh - 68px); background: var(--app-background); }
.app-body.with-horizontal-navigation { height: calc(100vh - 118px); }

.app-sider {
  border-right: 1px solid var(--app-border-subtle);
  background: var(--app-sidebar) !important;
}

.sider-version {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 42px;
  padding: 0 18px;
  color: var(--app-sidebar-text);
  opacity: 0.58;
  font-size: 9px;
  letter-spacing: 0.08em;
}

.sider-version.compact { justify-content: center; padding: 0; }
.sider-version small { font-size: 10px; }

.app-main-content-wrapper { height: 100%; background: var(--app-background); }
.page-content-inner-wrapper { min-height: 100%; padding: 0; box-sizing: border-box; }
.log-panel { height: 60vh; font-size: 13px; line-height: 1.6; }

.mobile-bottom-navigation { display: none; }

@media (max-width: 767px) {
  .app-header { height: 60px; padding: 0 12px; }
  .brand-copy span { display: none; }
  .brand-copy strong { font-size: 12px; }
  .brand-logo { width: 30px; height: 30px; }
  .app-body { height: calc(100vh - 60px); }
  .page-content-inner-wrapper {
    height: calc(100dvh - 60px);
    max-height: calc(100dvh - 60px);
    min-height: 0;
    padding: 0 0 76px;
    box-sizing: border-box;
    overflow-x: hidden;
    overflow-y: auto;
    overscroll-behavior-y: contain;
    -webkit-overflow-scrolling: touch;
  }

  .mobile-bottom-navigation {
    position: fixed;
    right: 10px;
    bottom: max(10px, env(safe-area-inset-bottom));
    left: 10px;
    z-index: 900;
    display: flex;
    justify-content: space-around;
    padding: 7px 6px;
    border: 1px solid var(--app-border-subtle);
    border-radius: 18px;
    background: color-mix(in srgb, var(--app-surface) 90%, transparent);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.22);
    backdrop-filter: blur(18px);
  }

  .mobile-bottom-navigation button {
    display: flex;
    min-width: 52px;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    padding: 4px 6px;
    border: 0;
    border-radius: 10px;
    color: var(--app-text-muted);
    background: transparent;
    font-size: 10px;
  }

  .mobile-bottom-navigation button.active { color: var(--app-primary); background: var(--app-primary-soft); }
}
</style>
