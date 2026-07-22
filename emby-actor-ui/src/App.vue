<!-- src/App.vue -->
<template>
  <n-config-provider :theme="isDarkTheme ? darkTheme : undefined" :theme-overrides="currentNaiveTheme" :locale="zhCN" :date-locale="dateZhCN">
    <n-message-provider :placement="messagePlacement">
      <n-dialog-provider>
        <AppContent />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { NConfigProvider, NMessageProvider, NDialogProvider, darkTheme, zhCN, dateZhCN } from 'naive-ui';
import AppContent from './AppContent.vue';

const isDarkTheme = ref(false);
const currentNaiveTheme = ref({});
const viewportWidth = ref(window.innerWidth);
const appRoot = document.getElementById('app');
const messagePlacement = computed(() => viewportWidth.value < 768 ? 'top' : 'bottom-right');

function handleNaiveThemeUpdate(event) {
  currentNaiveTheme.value = event.detail;
}

function handleDarkModeUpdate(event) {
  isDarkTheme.value = event.detail;
}

appRoot?.addEventListener('update-naive-theme', handleNaiveThemeUpdate);
appRoot?.addEventListener('update-dark-mode', handleDarkModeUpdate);

function updateViewportWidth() {
  viewportWidth.value = window.innerWidth;
}

onMounted(() => window.addEventListener('resize', updateViewportWidth));

const savedScale = localStorage.getItem('global_card_scale');
document.documentElement.style.setProperty('--card-scale', savedScale || '1');

onBeforeUnmount(() => {
  window.removeEventListener('resize', updateViewportWidth);
  appRoot?.removeEventListener('update-naive-theme', handleNaiveThemeUpdate);
  appRoot?.removeEventListener('update-dark-mode', handleDarkModeUpdate);
});
</script>

<style>
html, body { height: 100vh; margin: 0; padding: 0; font-family: Inter, "Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; overflow: hidden; }
.fullscreen-container { display: flex; justify-content: center; align-items: center; height: 100vh; width: 100%; }
.fullscreen-container { background-color: var(--app-background); }
@media (max-width: 767px) {
  .n-message-container { max-width: calc(100vw - 24px); }
  .n-message { white-space: normal; overflow-wrap: anywhere; }
}
</style>
