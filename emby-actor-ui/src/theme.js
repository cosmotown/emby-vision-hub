const THEME_STORAGE_KEY = 'moviepilot-theme-customizer';

export const moviePilotThemeOptions = [
  { label: '跟随系统', value: 'auto' },
  { label: '浅色', value: 'light' },
  { label: '深色', value: 'dark' },
  { label: '幻紫', value: 'purple' },
  { label: '透明', value: 'transparent' },
];

export const moviePilotPrimaryColors = [
  { name: 'Purple', value: '#8D51F9' },
  { name: 'Indigo', value: '#3F51B5' },
  { name: 'Blue', value: '#1976D2' },
  { name: 'Cyan', value: '#00BCD4' },
  { name: 'Teal', value: '#009688' },
  { name: 'Green', value: '#4CAF50' },
  { name: 'Amber', value: '#FFB400' },
  { name: 'Orange', value: '#FF9800' },
  { name: 'Coral', value: '#FF4C51' },
  { name: 'Pink', value: '#E91E63' },
  { name: 'Sky', value: '#16B1FF' },
  { name: 'Slate', value: '#607D8B' },
];

export const moviePilotLayoutOptions = [
  { label: '垂直', value: 'vertical' },
  { label: '折叠', value: 'collapsed' },
  { label: '水平', value: 'horizontal' },
];

export const moviePilotRadiusOptions = [
  { label: '无圆角', value: 'none' },
  { label: '小圆角', value: 'small' },
  { label: '默认', value: 'default' },
  { label: '大圆角', value: 'large' },
  { label: '更大圆角', value: 'extra' },
];

export const moviePilotSkinOptions = [
  { label: '无边框', value: 'default' },
  { label: '有边框', value: 'bordered' },
];

export const defaultThemeSettings = Object.freeze({
  theme: 'auto',
  primaryColor: '#8D51F9',
  skin: 'default',
  radius: 'default',
  shadow: '0',
  semiDarkMenu: false,
  layout: 'vertical',
  transparentOpacity: 0.3,
  transparentBlur: 10,
  transparentBackgroundPosterOpacity: 0,
  transparentBackgroundBlur: 16,
  transparentGlassQuality: 'lightweight',
});

const themeNames = new Set(moviePilotThemeOptions.map((item) => item.value));
const layoutNames = new Set(moviePilotLayoutOptions.map((item) => item.value));
const radiusNames = new Set(moviePilotRadiusOptions.map((item) => item.value));
const skinNames = new Set(moviePilotSkinOptions.map((item) => item.value));
const glassQualityNames = new Set(['lightweight', 'realtime']);

const clampNumber = (value, fallback, min, max) => {
  const numericValue = Number(value);
  return Number.isFinite(numericValue)
    ? Math.min(max, Math.max(min, numericValue))
    : fallback;
};

const radiusPixels = {
  none: '0px',
  small: '4px',
  default: '8px',
  large: '12px',
  extra: '16px',
};

const baseThemes = {
  light: {
    primary: '#8D51F9',
    background: '#F4F5FA',
    surface: '#FFFFFF',
    surfaceSoft: '#F9F8F9',
    sidebar: '#FFFFFF',
    text: '#3A3541',
    textMuted: '#6E6B7B',
    border: 'rgba(58, 53, 65, 0.12)',
    tableHeader: '#F9FAFC',
    overlay: '#3A3541',
    overlayOpacity: '0.5',
  },
  dark: {
    primary: '#6E66ED',
    background: '#0E1116',
    surface: '#14161F',
    surfaceSoft: '#373452',
    sidebar: '#14161F',
    text: '#E7E3FC',
    textMuted: 'rgba(231, 227, 252, 0.68)',
    border: 'rgba(231, 227, 252, 0.12)',
    tableHeader: '#14161F',
    overlay: '#191D21',
    overlayOpacity: '0.6',
  },
  purple: {
    primary: '#8D51F9',
    background: '#28243D',
    surface: '#312D4B',
    surfaceSoft: '#373452',
    sidebar: '#312D4B',
    text: '#E7E3FC',
    textMuted: 'rgba(231, 227, 252, 0.68)',
    border: 'rgba(231, 227, 252, 0.12)',
    tableHeader: '#3D3759',
    overlay: '#2C2942',
    overlayOpacity: '0.6',
  },
  transparent: {
    primary: '#A370F7',
    background: '#1C1C1C',
    surface: 'rgba(30, 30, 30, 0.30)',
    surfaceSoft: 'rgba(30, 30, 30, 0.20)',
    sidebar: 'rgba(30, 30, 30, 0.20)',
    text: '#E7E3FC',
    textMuted: 'rgba(255, 255, 255, 0.65)',
    border: 'rgba(255, 255, 255, 0.12)',
    tableHeader: 'rgba(30, 30, 30, 0.30)',
    overlay: '#000000',
    overlayOpacity: '0.7',
  },
};

export function getMoviePilotThemePrimary(themeName) {
  const resolvedTheme = resolveThemeName(themeName);
  return (baseThemes[resolvedTheme] || baseThemes.light).primary;
}

const normalizeHex = (value) => (
  typeof value === 'string' && /^#[\da-f]{6}$/i.test(value)
    ? value.toUpperCase()
    : defaultThemeSettings.primaryColor
);

export function normalizeThemeSettings(settings = {}) {
  const shadow = String(settings.shadow ?? defaultThemeSettings.shadow);
  const numericShadow = Number.parseInt(shadow, 10);

  return {
    theme: themeNames.has(settings.theme) ? settings.theme : defaultThemeSettings.theme,
    primaryColor: normalizeHex(settings.primaryColor),
    skin: skinNames.has(settings.skin) ? settings.skin : defaultThemeSettings.skin,
    radius: radiusNames.has(settings.radius) ? settings.radius : defaultThemeSettings.radius,
    shadow: Number.isInteger(numericShadow) && numericShadow >= 0 && numericShadow <= 24
      ? String(numericShadow)
      : defaultThemeSettings.shadow,
    semiDarkMenu: typeof settings.semiDarkMenu === 'boolean'
      ? settings.semiDarkMenu
      : defaultThemeSettings.semiDarkMenu,
    layout: layoutNames.has(settings.layout) ? settings.layout : defaultThemeSettings.layout,
    transparentOpacity: clampNumber(
      settings.transparentOpacity,
      defaultThemeSettings.transparentOpacity,
      0,
      1,
    ),
    transparentBlur: clampNumber(
      settings.transparentBlur,
      defaultThemeSettings.transparentBlur,
      0,
      30,
    ),
    transparentBackgroundPosterOpacity: clampNumber(
      settings.transparentBackgroundPosterOpacity,
      defaultThemeSettings.transparentBackgroundPosterOpacity,
      0,
      1,
    ),
    transparentBackgroundBlur: clampNumber(
      settings.transparentBackgroundBlur,
      defaultThemeSettings.transparentBackgroundBlur,
      0,
      30,
    ),
    transparentGlassQuality: glassQualityNames.has(settings.transparentGlassQuality)
      ? settings.transparentGlassQuality
      : defaultThemeSettings.transparentGlassQuality,
  };
}

export function loadThemeSettings() {
  try {
    const moviePilotSettings = JSON.parse(localStorage.getItem(THEME_STORAGE_KEY) || '{}');
    if (Object.keys(moviePilotSettings).length > 0) {
      return normalizeThemeSettings(moviePilotSettings);
    }
  } catch (error) {
    console.warn('读取 MoviePilot 主题设置失败，将使用默认主题。', error);
  }

  // 只迁移旧版明暗偏好，不再迁移旧版 Toolkit 皮肤。
  const legacyDark = localStorage.getItem('isDark');
  return normalizeThemeSettings({ theme: legacyDark === 'true' ? 'dark' : 'auto' });
}

export function saveThemeSettings(settings) {
  localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify(normalizeThemeSettings(settings)));
}

export function resolveThemeName(themeName) {
  if (themeName !== 'auto') return themeName;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function isDarkTheme(themeName) {
  return ['dark', 'purple', 'transparent'].includes(resolveThemeName(themeName));
}

function hexToRgb(hex) {
  const value = normalizeHex(hex).slice(1);
  return [0, 2, 4].map((offset) => Number.parseInt(value.slice(offset, offset + 2), 16));
}

function alpha(hex, opacity) {
  const [red, green, blue] = hexToRgb(hex);
  return `rgba(${red}, ${green}, ${blue}, ${opacity})`;
}

function colorShift(hex, amount) {
  const [red, green, blue] = hexToRgb(hex);
  const shift = (channel) => Math.max(0, Math.min(255, channel + amount));
  return `#${[shift(red), shift(green), shift(blue)]
    .map((channel) => channel.toString(16).padStart(2, '0'))
    .join('')}`;
}

export function buildTheme(settings) {
  const normalized = normalizeThemeSettings(settings);
  const resolvedTheme = resolveThemeName(normalized.theme);
  const dark = ['dark', 'purple', 'transparent'].includes(resolvedTheme);
  const palette = baseThemes[resolvedTheme] || baseThemes.light;
  // MoviePilot 为每种主题配置了不同的默认主色；用户选择自定义色后再覆盖默认值。
  const primary = normalized.primaryColor === defaultThemeSettings.primaryColor
    ? palette.primary
    : normalized.primaryColor;
  const radius = radiusPixels[normalized.radius];
  const shadowLevel = Number.parseInt(normalized.shadow, 10);
  const showBorder = normalized.skin === 'bordered';
  const cardShadow = shadowLevel === 0
    ? 'none'
    : `0 ${Math.max(2, Math.round(shadowLevel / 3))}px ${Math.max(8, shadowLevel * 2)}px rgba(0, 0, 0, ${dark ? 0.34 : 0.14})`;
  const sidebar = normalized.semiDarkMenu && !dark ? baseThemes.dark.sidebar : palette.sidebar;
  const sidebarText = normalized.semiDarkMenu && !dark ? baseThemes.dark.text : palette.text;
  const border = showBorder ? palette.border : 'transparent';
  const transparentOpacity = normalized.transparentOpacity;
  const transparentOpacityLight = Math.min(1, transparentOpacity * 0.67);
  const transparentOpacityHeavy = Math.min(1, transparentOpacity * 1.67);

  return {
    settings: normalized,
    resolvedTheme,
    dark,
    custom: {
      '--app-background': palette.background,
      '--app-surface': palette.surface,
      '--app-surface-soft': palette.surfaceSoft,
      '--app-sidebar': sidebar,
      '--app-sidebar-text': sidebarText,
      '--app-text': palette.text,
      '--app-text-muted': palette.textMuted,
      '--app-border': border,
      '--app-border-subtle': palette.border,
      '--app-radius': radius,
      '--app-shadow': cardShadow,
      '--app-primary': primary,
      '--app-primary-soft': alpha(primary, dark ? 0.22 : 0.12),
      '--app-primary-glow': alpha(primary, 0.28),
      '--app-table-header': palette.tableHeader,
      '--app-overlay': palette.overlay,
      '--app-overlay-opacity': palette.overlayOpacity,
      '--transparent-opacity': transparentOpacity.toString(),
      '--transparent-opacity-light': transparentOpacityLight.toString(),
      '--transparent-opacity-heavy': transparentOpacityHeavy.toString(),
      '--transparent-blur': `${normalized.transparentBlur}px`,
      '--transparent-blur-light': `${normalized.transparentBlur * 0.6}px`,
      '--transparent-blur-heavy': `${normalized.transparentBlur * 1.6}px`,
      '--transparent-background-poster-opacity': (1 - normalized.transparentBackgroundPosterOpacity).toString(),
      '--transparent-background-blur': `${normalized.transparentBackgroundBlur}px`,
      '--app-backdrop-blur': resolvedTheme === 'transparent' ? `${normalized.transparentBackgroundBlur}px` : '0px',
      '--app-backdrop-scale': resolvedTheme === 'transparent' ? '1.03' : '1',
      '--app-backdrop-shade': '0',
      '--card-bg-color': palette.surface,
      '--modal-solid-bg-color': resolvedTheme === 'transparent' ? 'rgba(30, 30, 30, 0.50)' : palette.surface,
      '--card-border-color': border,
      '--card-shadow-color': dark ? 'rgba(0, 0, 0, 0.34)' : 'rgba(0, 0, 0, 0.12)',
      '--accent-color': primary,
      '--accent-glow-color': alpha(primary, 0.18),
      '--text-color': palette.text,
    },
    naive: {
      common: {
        primaryColor: primary,
        primaryColorHover: colorShift(primary, 18),
        primaryColorPressed: colorShift(primary, -18),
        primaryColorSuppl: primary,
        bodyColor: palette.background,
        cardColor: palette.surface,
        modalColor: resolvedTheme === 'transparent' ? 'rgba(30, 30, 30, 0.50)' : palette.surface,
        popoverColor: resolvedTheme === 'transparent' ? 'rgba(30, 30, 30, 0.50)' : palette.surface,
        textColorBase: palette.text,
        textColor1: palette.text,
        textColor2: palette.textMuted,
        textColor3: palette.textMuted,
        dividerColor: palette.border,
        borderColor: palette.border,
        inputColor: resolvedTheme === 'transparent' ? 'transparent' : palette.surface,
        tableColor: palette.surface,
        actionColor: palette.surfaceSoft,
        hoverColor: alpha(primary, dark ? 0.10 : 0.04),
        borderRadius: radius,
      },
      Card: {
        color: palette.surface,
        borderColor: border,
        borderRadius: radius,
      },
      Layout: {
        color: palette.background,
        headerColor: palette.surface,
        siderColor: sidebar,
      },
      Menu: {
        color: 'transparent',
        itemColorActive: primary,
        itemColorActiveHover: colorShift(primary, 10),
        itemTextColor: sidebarText,
        itemIconColor: sidebarText,
        itemTextColorActive: '#FFFFFF',
        itemIconColorActive: '#FFFFFF',
        itemTextColorActiveHover: '#FFFFFF',
        itemIconColorActiveHover: '#FFFFFF',
        itemBorderRadius: radius,
      },
      Button: {
        borderRadiusMedium: radius,
        textColorPrimary: '#FFFFFF',
        textColorHoverPrimary: '#FFFFFF',
        textColorPressedPrimary: '#FFFFFF',
        textColorFocusPrimary: '#FFFFFF',
        textColorDisabledPrimary: '#FFFFFF',
      },
      Radio: {
        buttonColorActive: primary,
        buttonTextColorActive: '#FFFFFF',
        buttonBorderColorActive: primary,
        buttonBoxShadow: `inset 0 0 0 1px ${palette.border}`,
        buttonBoxShadowHover: `inset 0 0 0 1px ${primary}`,
        buttonBoxShadowFocus: `inset 0 0 0 1px ${primary}, 0 0 0 2px ${alpha(primary, 0.22)}`,
      },
      Input: {
        borderRadius: radius,
        color: resolvedTheme === 'transparent' ? 'transparent' : palette.surface,
        colorFocus: resolvedTheme === 'transparent' ? 'transparent' : palette.surface,
      },
      Select: { peers: { InternalSelection: { borderRadius: radius } } },
      DataTable: {
        thColor: palette.tableHeader,
        tdColor: resolvedTheme === 'transparent' ? 'transparent' : palette.surface,
        tdColorHover: alpha(primary, dark ? 0.10 : 0.04),
        borderColor: palette.border,
      },
      Drawer: { color: resolvedTheme === 'transparent' ? 'rgba(30, 30, 30, 0.50)' : palette.surface },
    },
  };
}

export function applyThemeToDocument(theme) {
  const root = document.documentElement;
  const body = document.body;

  Object.entries(theme.custom).forEach(([key, value]) => root.style.setProperty(key, value));
  root.classList.toggle('dark', theme.dark);
  root.classList.toggle('light', !theme.dark);
  const transparentTheme = theme.resolvedTheme === 'transparent';
  root.classList.toggle(
    'transparent-glass-lightweight',
    transparentTheme && theme.settings.transparentGlassQuality === 'lightweight',
  );
  root.classList.toggle(
    'transparent-glass-realtime',
    transparentTheme && theme.settings.transparentGlassQuality === 'realtime',
  );
  root.classList.toggle('transparent-blur-disabled', transparentTheme && theme.settings.transparentBlur <= 0);
  root.classList.toggle(
    'transparent-background-blur-disabled',
    transparentTheme && theme.settings.transparentBackgroundBlur <= 0,
  );
  root.dataset.theme = theme.resolvedTheme;
  root.dataset.themePreference = theme.settings.theme;
  root.dataset.themeLayout = theme.settings.layout;
  root.dataset.themeRadius = theme.settings.radius;
  root.dataset.themeShadow = theme.settings.shadow;
  root.dataset.themeSkin = theme.settings.skin;
  root.dataset.themeSemiDarkMenu = String(theme.settings.semiDarkMenu);
  root.style.colorScheme = theme.dark ? 'dark' : 'light';

  if (body) {
    body.dataset.theme = theme.resolvedTheme;
    body.dataset.themePreference = theme.settings.theme;
    body.dataset.themeLayout = theme.settings.layout;
    body.style.colorScheme = theme.dark ? 'dark' : 'light';
  }
}

export function resetThemeSettings() {
  return { ...defaultThemeSettings };
}
