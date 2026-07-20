<!-- src/App.vue -->
<template>
  <n-config-provider :theme="isDarkTheme ? darkTheme : undefined" :theme-overrides="currentNaiveTheme" :locale="zhCN" :date-locale="dateZhCN">
    <n-message-provider placement="bottom-right">
      <n-dialog-provider>
        <AppContent />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script setup>
import { onBeforeUnmount, ref } from 'vue';
import { NConfigProvider, NMessageProvider, NDialogProvider, darkTheme, zhCN, dateZhCN } from 'naive-ui';
import AppContent from './AppContent.vue';

const isDarkTheme = ref(false);
const currentNaiveTheme = ref({});
const appRoot = document.getElementById('app');

function handleNaiveThemeUpdate(event) {
  currentNaiveTheme.value = event.detail;
}

function handleDarkModeUpdate(event) {
  isDarkTheme.value = event.detail;
}

appRoot?.addEventListener('update-naive-theme', handleNaiveThemeUpdate);
appRoot?.addEventListener('update-dark-mode', handleDarkModeUpdate);

const savedScale = localStorage.getItem('global_card_scale');
document.documentElement.style.setProperty('--card-scale', savedScale || '1');

onBeforeUnmount(() => {
  appRoot?.removeEventListener('update-naive-theme', handleNaiveThemeUpdate);
  appRoot?.removeEventListener('update-dark-mode', handleDarkModeUpdate);
});
</script>

<style>
html, body { height: 100vh; margin: 0; padding: 0; font-family: Inter, "Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; overflow: hidden; }
.fullscreen-container { display: flex; justify-content: center; align-items: center; height: 100vh; width: 100%; }
.fullscreen-container { background-color: var(--app-background); }
</style>
