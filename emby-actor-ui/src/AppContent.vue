<template>
  <MainLayout
    v-if="showMainLayout && isReady"
    :theme-settings="themeSettings"
    :task-status="backgroundTaskStatus"
    @update:theme-settings="updateThemeSettings"
    @reset-theme-settings="resetTheme"
  />

  <div v-else-if="isReady" class="fullscreen-container">
    <router-view />
  </div>

  <div v-else class="fullscreen-container">
    <n-spin size="large" />
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { NSpin } from 'naive-ui';
import axios from 'axios';
import { useAuthStore } from './stores/auth';
import MainLayout from './MainLayout.vue';
import {
  applyThemeToDocument,
  buildTheme,
  loadThemeSettings,
  normalizeThemeSettings,
  resetThemeSettings,
  saveThemeSettings,
} from './theme.js';

const route = useRoute();
const authStore = useAuthStore();
const showMainLayout = computed(() => !route.meta.public);
const themeSettings = ref(loadThemeSettings());
const isReady = ref(false);
const backgroundTaskStatus = ref({ is_running: false, current_action: '空闲' });
const systemThemeQuery = window.matchMedia?.('(prefers-color-scheme: dark)');
let statusIntervalId = null;
let backdropLoaded = false;

async function loadMovieBackdrop() {
  if (backdropLoaded || !authStore.isLoggedIn) return;
  try {
    const response = await axios.get('/api/discover/daily_recommendation');
    const pool = Array.isArray(response.data?.pool) ? response.data.pool : [];
    const media = pool.find((item) => item?.backdrop_path) || pool.find((item) => item?.poster_path);
    const imagePath = media?.backdrop_path || media?.poster_path;
    if (typeof imagePath !== 'string' || !/^\/[\w/.-]+$/.test(imagePath)) return;
    const size = media?.backdrop_path ? 'original' : 'w1280';
    const imageUrl = `https://image.tmdb.org/t/p/${size}${imagePath}`;
    document.documentElement.style.setProperty('--app-backdrop-image', `url("${imageUrl}")`);
    backdropLoaded = true;
  } catch (error) {
    console.debug('透明主题背景图暂不可用，将使用内置背景。');
  }
}

function applyCurrentTheme() {
  const theme = buildTheme(themeSettings.value);
  applyThemeToDocument(theme);

  const app = document.getElementById('app');
  app?.dispatchEvent(new CustomEvent('update-naive-theme', { detail: theme.naive }));
  app?.dispatchEvent(new CustomEvent('update-dark-mode', { detail: theme.dark }));
}

function updateThemeSettings(settings) {
  themeSettings.value = normalizeThemeSettings(settings);
}

function resetTheme() {
  themeSettings.value = resetThemeSettings();
}

function handleSystemThemeChange() {
  if (themeSettings.value.theme === 'auto') applyCurrentTheme();
}

watch(themeSettings, (settings) => {
  saveThemeSettings(settings);
  applyCurrentTheme();
}, { deep: true });

watch(() => authStore.isLoggedIn, (isLoggedIn) => {
  if (isLoggedIn && !statusIntervalId) {
    loadMovieBackdrop();
    const fetchStatus = async () => {
      try {
        const response = await axios.get('/api/status');
        backgroundTaskStatus.value = response.data;
      } catch (error) {
        console.error('获取状态失败:', error);
      }
    };
    fetchStatus();
    statusIntervalId = setInterval(fetchStatus, 2000);
  } else if (!isLoggedIn && statusIntervalId) {
    clearInterval(statusIntervalId);
    statusIntervalId = null;
  }
}, { immediate: true });

onMounted(() => {
  applyCurrentTheme();
  systemThemeQuery?.addEventListener('change', handleSystemThemeChange);
  isReady.value = true;
});

onBeforeUnmount(() => {
  if (statusIntervalId) clearInterval(statusIntervalId);
  systemThemeQuery?.removeEventListener('change', handleSystemThemeChange);
});
</script>
