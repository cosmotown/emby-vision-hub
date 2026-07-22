<template>
  <n-layout class="mobile-more-page" content-style="padding: 16px 14px 104px;">
    <n-page-header title="更多" subtitle="全部功能与账户设置" />

    <section v-for="section in navigationSections" :key="section.key" class="more-section">
      <n-text depth="3" class="section-label">{{ section.label }}</n-text>
      <div class="more-grid">
        <button
          v-for="item in section.items"
          :key="item.key"
          type="button"
          class="more-item"
          @click="openRoute(item.key)"
        >
          <span class="more-icon"><n-icon :component="item.icon" size="22" /></span>
          <span>{{ item.label }}</span>
        </button>
      </div>
    </section>

    <section class="more-section">
      <n-text depth="3" class="section-label">账户与系统</n-text>
      <div class="more-list">
        <button type="button" @click="openRoute('UserCenter')">
          <n-icon :component="PersonCircleOutline" size="21" />
          <span>个人中心</span>
          <n-icon :component="ChevronForwardOutline" size="16" />
        </button>
        <button v-if="authStore.isAdmin" type="button" @click="openRoute('settings-general')">
          <n-icon :component="OptionsOutline" size="21" />
          <span>系统设置</span>
          <n-icon :component="ChevronForwardOutline" size="16" />
        </button>
        <button type="button" @click="openTheme">
          <n-icon :component="ColorPaletteOutline" size="21" />
          <span>主题与外观</span>
          <n-icon :component="ChevronForwardOutline" size="16" />
        </button>
        <button v-if="authStore.isAdmin" type="button" @click="openRoute('Releases')">
          <n-icon :component="InformationCircleOutline" size="21" />
          <span>关于 EVH</span>
          <n-icon :component="ChevronForwardOutline" size="16" />
        </button>
        <button type="button" @click="openHelp">
          <n-icon :component="BookOutline" size="21" />
          <span>帮助文档</span>
          <n-icon :component="OpenOutline" size="16" />
        </button>
        <button type="button" class="logout-item" @click="logout">
          <n-icon :component="LogOutOutline" size="21" />
          <span>退出登录</span>
        </button>
      </div>
    </section>
  </n-layout>
</template>

<script setup>
import { computed } from 'vue';
import { useRouter } from 'vue-router';
import { NIcon, NLayout, NPageHeader, NText } from 'naive-ui';
import {
  BookOutline,
  ChevronForwardOutline,
  ColorPaletteOutline,
  InformationCircleOutline,
  LogOutOutline,
  OpenOutline,
  OptionsOutline,
  PersonCircleOutline,
} from '@vicons/ionicons5';
import { getVisibleNavigation, getVisibleTopNavigation } from '../navigation.js';
import { useAuthStore } from '../stores/auth';

const router = useRouter();
const authStore = useAuthStore();
const navigationSections = computed(() => {
  const topItems = getVisibleTopNavigation(authStore);
  const sections = getVisibleNavigation(authStore);
  return topItems.length
    ? [{ label: '概览', key: 'section-overview', items: topItems }, ...sections]
    : sections;
});

function openRoute(name) {
  router.push({ name });
}

function openTheme() {
  window.dispatchEvent(new CustomEvent('evh:open-theme-customizer'));
}

function openHelp() {
  window.open('https://github.com/cosmotown/emby-vision-hub', '_blank');
}

async function logout() {
  await authStore.logout();
  router.push({ name: 'Login' });
}
</script>

<style scoped>
.mobile-more-page { min-height: 100%; background: transparent; }
.more-section { margin-top: 22px; }
.section-label { display: block; margin: 0 4px 9px; font-size: 12px; }
.more-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.more-item,
.more-list button {
  border: 1px solid var(--app-border-subtle);
  color: var(--app-text);
  background: var(--app-surface);
}
.more-item {
  display: flex;
  min-width: 0;
  min-height: 82px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 10px 4px;
  border-radius: 14px;
  font-size: 12px;
}
.more-icon {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border-radius: 11px;
  color: var(--app-primary);
  background: var(--app-primary-soft);
}
.more-list { overflow: hidden; border: 1px solid var(--app-border-subtle); border-radius: 15px; }
.more-list button {
  display: grid;
  width: 100%;
  min-height: 52px;
  grid-template-columns: 26px 1fr auto;
  align-items: center;
  gap: 8px;
  padding: 0 14px;
  border: 0;
  border-bottom: 1px solid var(--app-border-subtle);
  text-align: left;
}
.more-list button:last-child { border-bottom: 0; }
.more-list .logout-item { color: var(--app-danger, #d03050); }

@media (max-width: 380px) {
  .more-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
</style>
