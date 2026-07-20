import {
  AlbumsOutline,
  AnalyticsOutline,
  ArchiveOutline,
  BookmarksOutline,
  ColorPaletteOutline,
  CreateOutline,
  FilmOutline,
  HeartOutline,
  InformationCircleOutline,
  ListOutline,
  OptionsOutline,
  PeopleCircleOutline,
  PeopleOutline,
  PersonCircleOutline,
  PieChartOutline,
  PricetagOutline,
  SparklesOutline,
  TimerOutline,
  TrashBinOutline,
} from '@vicons/ionicons5';

const route = (label, key, icon, access = 'user') => ({ label, key, icon, access });

export const topNavigationItems = [
  route('数据看板', 'DatabaseStats', AnalyticsOutline, 'admin'),
];

export const navigationSections = [
  {
    label: '发现',
    key: 'section-discovery',
    items: [
      route('影视探索', 'Discover', FilmOutline),
      route('播放统计', 'EmbyStats', PieChartOutline, 'admin'),
    ],
  },
  {
    label: '订阅',
    key: 'section-subscriptions',
    access: 'admin',
    items: [
      route('智能追剧', 'Watchlist', HeartOutline, 'admin'),
      route('演员订阅', 'ActorSubscriptions', PeopleOutline, 'admin'),
      route('统一订阅', 'UnifiedSubscriptions', ArchiveOutline, 'admin'),
    ],
  },
  {
    label: '整理',
    key: 'section-management',
    access: 'admin',
    items: [
      route('原生合集', 'Collections', AlbumsOutline, 'admin'),
      route('自建合集', 'CustomCollectionsManager', CreateOutline, 'admin'),
      route('媒体整理', 'ResubscribePage', SparklesOutline, 'admin'),
      route('媒体去重', 'MediaCleanupPage', TrashBinOutline, 'admin'),
      route('人物清理', 'PersonCleanupPage', PeopleOutline, 'admin'),
    ],
  },
  {
    label: '工作流',
    key: 'section-workflows',
    access: 'admin',
    items: [
      route('手动处理', 'ReviewList', ListOutline, 'admin'),
      route('自动标签', 'AutoTaggingPage', PricetagOutline, 'admin'),
      route('封面生成', 'CoverGeneratorConfig', ColorPaletteOutline, 'admin'),
      route('任务中心', 'settings-scheduler', TimerOutline, 'admin'),
    ],
  },
];

function canAccess(access, authStore) {
  if (access === 'admin') return authStore.isAdmin;
  return authStore.isLoggedIn;
}

export function getVisibleNavigation(authStore) {
  return navigationSections
    .filter((section) => !section.access || canAccess(section.access, authStore))
    .map((section) => ({
      ...section,
      items: section.items.filter((item) => canAccess(item.access, authStore)),
    }))
    .filter((section) => section.items.length > 0);
}

export function getVisibleTopNavigation(authStore) {
  return topNavigationItems.filter((item) => canAccess(item.access, authStore));
}

export const mobilePrimaryRoutes = [
  { label: '首页', key: 'DatabaseStats', icon: AnalyticsOutline, access: 'admin' },
  { label: '探索', key: 'Discover', icon: FilmOutline, access: 'user' },
  { label: '订阅', key: 'Watchlist', icon: BookmarksOutline, access: 'admin' },
  { label: '我的', key: 'UserCenter', icon: PersonCircleOutline, access: 'user' },
];

export function getVisibleMobileRoutes(authStore) {
  return mobilePrimaryRoutes.filter((item) => canAccess(item.access, authStore));
}
