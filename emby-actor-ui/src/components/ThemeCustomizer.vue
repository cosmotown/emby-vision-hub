<template>
  <n-drawer
    :show="show"
    :width="drawerWidth"
    placement="right"
    @update:show="emit('update:show', $event)"
  >
    <n-drawer-content title="主题定制" closable>
      <div class="theme-customizer-subtitle">实时自定义与预览</div>

      <section class="theme-setting-section">
        <label>主题</label>
        <n-radio-group
          :value="modelValue.theme"
          class="theme-option-grid"
          @update:value="update('theme', $event)"
        >
          <div
            v-for="option in moviePilotThemeOptions"
            :key="option.value"
            class="theme-option-cell"
          >
            <n-radio-button :value="option.value">
              {{ option.label }}
            </n-radio-button>
          </div>
        </n-radio-group>
      </section>

      <section class="theme-setting-section">
        <label>色调</label>
        <div class="primary-color-grid">
          <n-tooltip v-for="color in moviePilotPrimaryColors" :key="color.value">
            <template #trigger>
              <button
                type="button"
                class="primary-color-button"
                :class="{ active: displayedPrimaryColor === color.value }"
                :style="{ '--swatch-color': color.value }"
                :aria-label="`使用 ${color.name} 色调`"
                @click="update('primaryColor', color.value)"
              />
            </template>
            {{ color.name }}
          </n-tooltip>
          <n-color-picker
            :value="displayedPrimaryColor"
            :show-alpha="false"
            size="small"
            @update:value="update('primaryColor', $event)"
          />
        </div>
      </section>

      <section v-if="modelValue.theme === 'transparent'" class="theme-setting-section transparency-settings">
        <div class="setting-heading-row">
          <label>透明度调整</label>
          <span>{{ activeTransparencyPresetLabel }}</span>
        </div>
        <n-radio-group
          :value="activeTransparencyPreset"
          class="transparency-preset-row"
          @update:value="applyTransparencyPreset"
        >
          <n-radio-button
            v-for="preset in transparencyPresets"
            :key="preset.value"
            :value="preset.value"
          >
            {{ preset.label }}
          </n-radio-button>
        </n-radio-group>

        <div class="setting-heading-row">
          <label>底色透明度</label>
          <span>{{ Math.round(Number(modelValue.transparentOpacity) * 100) }}%</span>
        </div>
        <n-slider
          :value="Number(modelValue.transparentOpacity)"
          :min="0"
          :max="1"
          :step="0.05"
          @update:value="update('transparentOpacity', $event)"
        />

        <div class="setting-heading-row">
          <label>玻璃模糊</label>
          <span>{{ Number(modelValue.transparentBlur) }}px</span>
        </div>
        <n-slider
          :value="Number(modelValue.transparentBlur)"
          :min="0"
          :max="30"
          :step="1"
          @update:value="update('transparentBlur', $event)"
        />

        <div class="setting-heading-row">
          <label>背景透明度</label>
          <span>{{ Math.round(Number(modelValue.transparentBackgroundPosterOpacity) * 100) }}%</span>
        </div>
        <n-slider
          :value="Number(modelValue.transparentBackgroundPosterOpacity)"
          :min="0"
          :max="1"
          :step="0.05"
          @update:value="update('transparentBackgroundPosterOpacity', $event)"
        />

        <div class="setting-heading-row">
          <label>背景磨砂</label>
          <span>{{ Number(modelValue.transparentBackgroundBlur) }}px</span>
        </div>
        <n-slider
          :value="Number(modelValue.transparentBackgroundBlur)"
          :min="0"
          :max="30"
          :step="1"
          @update:value="update('transparentBackgroundBlur', $event)"
        />

        <div class="select-setting">
          <label>玻璃效果质量</label>
          <n-select
            :value="modelValue.transparentGlassQuality"
            :options="glassQualityOptions"
            @update:value="update('transparentGlassQuality', $event)"
          />
        </div>
      </section>

      <section class="theme-setting-section two-column-settings">
        <div class="select-setting">
          <label>边框</label>
          <n-select
            :value="modelValue.skin"
            :options="moviePilotSkinOptions"
            @update:value="update('skin', $event)"
          />
        </div>
        <div class="select-setting">
          <label>圆角</label>
          <n-select
            :value="modelValue.radius"
            :options="moviePilotRadiusOptions"
            @update:value="update('radius', $event)"
          />
        </div>
      </section>

      <section class="theme-setting-section">
        <div class="setting-heading-row">
          <label>阴影</label>
          <span>层级 {{ modelValue.shadow }}</span>
        </div>
        <n-slider
          :value="Number(modelValue.shadow)"
          :min="0"
          :max="24"
          :step="1"
          @update:value="update('shadow', String($event))"
        />
      </section>

      <section class="theme-setting-section">
        <div class="setting-heading-row">
          <label>半暗菜单</label>
          <n-switch
            :value="modelValue.semiDarkMenu"
            @update:value="update('semiDarkMenu', $event)"
          />
        </div>
      </section>

      <section class="theme-setting-section">
        <label>布局</label>
        <n-radio-group
          :value="modelValue.layout"
          class="layout-option-row"
          @update:value="update('layout', $event)"
        >
          <div
            v-for="option in moviePilotLayoutOptions"
            :key="option.value"
            class="layout-option-cell"
          >
            <n-radio-button :value="option.value">
              {{ option.label }}
            </n-radio-button>
          </div>
        </n-radio-group>
      </section>

      <template #footer>
        <n-button block secondary @click="emit('reset')">重置主题定制</n-button>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>

<script setup>
import { computed } from 'vue';
import {
  NButton,
  NColorPicker,
  NDrawer,
  NDrawerContent,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSlider,
  NSwitch,
  NTooltip,
} from 'naive-ui';
import {
  defaultThemeSettings,
  getMoviePilotThemePrimary,
  moviePilotLayoutOptions,
  moviePilotPrimaryColors,
  moviePilotRadiusOptions,
  moviePilotSkinOptions,
  moviePilotThemeOptions,
} from '../theme.js';

const props = defineProps({
  show: Boolean,
  mobile: Boolean,
  modelValue: {
    type: Object,
    required: true,
  },
});

const emit = defineEmits(['update:show', 'update:model-value', 'reset']);
const drawerWidth = computed(() => (
  props.mobile ? Math.min(360, Math.floor(window.innerWidth * 0.92)) : 360
));
const displayedPrimaryColor = computed(() => (
  props.modelValue.primaryColor === defaultThemeSettings.primaryColor
    ? getMoviePilotThemePrimary(props.modelValue.theme)
    : props.modelValue.primaryColor
));
const transparencyPresets = [
  { label: '低', value: 'low', opacity: 0.6, blur: 5 },
  { label: '中', value: 'medium', opacity: 0.3, blur: 10 },
  { label: '高', value: 'high', opacity: 0.1, blur: 15 },
];
const glassQualityOptions = [
  { label: '轻量', value: 'lightweight' },
  { label: '实时', value: 'realtime' },
];
const activeTransparencyPreset = computed(() => {
  const opacity = Number(props.modelValue.transparentOpacity);
  const blur = Number(props.modelValue.transparentBlur);
  return transparencyPresets.find((preset) => (
    Math.abs(preset.opacity - opacity) < 0.01 && Math.abs(preset.blur - blur) < 0.1
  ))?.value || '';
});
const activeTransparencyPresetLabel = computed(() => (
  transparencyPresets.find((preset) => preset.value === activeTransparencyPreset.value)?.label || '自定义'
));

function update(key, value) {
  emit('update:model-value', { ...props.modelValue, [key]: value });
}

function applyTransparencyPreset(value) {
  const preset = transparencyPresets.find((item) => item.value === value);
  if (!preset) return;
  emit('update:model-value', {
    ...props.modelValue,
    transparentOpacity: preset.opacity,
    transparentBlur: preset.blur,
  });
}
</script>

<style scoped>
.theme-customizer-subtitle {
  color: var(--app-text-muted);
  margin: -8px 0 24px;
}

.theme-setting-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 24px;
}

.theme-setting-section label {
  font-size: 13px;
  font-weight: 600;
}

.theme-option-grid {
  display: grid !important;
  width: 100%;
  height: auto !important;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.theme-option-cell,
.layout-option-cell {
  min-width: 0;
}

.theme-option-grid :deep(.n-radio-button),
.layout-option-row :deep(.n-radio-button) {
  width: 100%;
  margin: 0 !important;
  border-radius: var(--app-radius) !important;
  text-align: center;
}

.layout-option-row :deep(.n-radio-button) {
  text-align: center;
}

.primary-color-grid {
  display: grid;
  grid-template-columns: repeat(6, 32px);
  gap: 12px;
  align-items: center;
}

.primary-color-button {
  position: relative;
  width: 30px;
  height: 30px;
  border: 2px solid transparent;
  border-radius: 50%;
  background: var(--swatch-color);
  cursor: pointer;
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}

.primary-color-button:hover,
.primary-color-button.active {
  transform: scale(1.08);
  box-shadow: 0 0 0 3px var(--app-surface), 0 0 0 5px var(--swatch-color);
}

.two-column-settings {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.select-setting {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 8px;
}

.setting-heading-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--app-text-muted);
  font-size: 12px;
}

.layout-option-row {
  display: grid !important;
  width: 100%;
  height: auto !important;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.transparency-settings {
  padding: 16px;
  border: 1px solid var(--app-border-subtle);
  border-radius: var(--app-radius);
  background: var(--app-surface-soft);
}

.transparency-preset-row {
  display: grid !important;
  width: 100%;
  height: auto !important;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.transparency-preset-row :deep(.n-radio-button) {
  width: 100%;
  margin: 0 !important;
  border-radius: var(--app-radius) !important;
  text-align: center;
}
</style>
