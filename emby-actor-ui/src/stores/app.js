// src/stores/app.js

import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import axios from 'axios';

export const useAppStore = defineStore('app', () => {
  // --- State ---
  const currentVersion = ref('');
  const latestVersion = ref('');
  const releases = ref([]);

  // --- Getters (Computed) ---
  const isUpdateAvailable = computed(() => {
  // 1. 确保两个版本号都已获取
  if (!latestVersion.value || !currentVersion.value) {
    return false;
  }

  const parseStableVersion = (value) => {
    const match = String(value || '').trim().match(/^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/);
    return match ? match.slice(1).map(Number) : null;
  };
  const latest = parseStableVersion(latestVersion.value);
  const current = parseStableVersion(currentVersion.value);
  if (!latest || !current) return false;
  for (let index = 0; index < latest.length; index += 1) {
    if (latest[index] !== current[index]) return latest[index] > current[index];
  }
  return false;
});

  // --- Actions ---
  async function fetchVersionInfo() {
    try {
      const response = await axios.get('/api/system/about_info');
      currentVersion.value = response.data.current_version;
      releases.value = response.data.releases;
      
      latestVersion.value = response.data.latest_stable_version || '';
    } catch (error) {
      console.error('Failed to fetch version info:', error);
    }
  }

  return {
    currentVersion,
    latestVersion,
    releases,
    isUpdateAvailable,
    fetchVersionInfo,
  };
});
