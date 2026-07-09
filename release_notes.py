# release_notes.py

CUSTOM_RELEASES = [
    {
        "version": "v7.0.9",
        "published_at": "2026-07-09T01:10:00+08:00",
        "url": "https://github.com/cosmotown/emby-toolkit/tree/v7.0.9",
        "changelog": """## 动态封面架构重做

### 修复
- 动态 ChillPoster 不再在 Web 主进程内渲染，避免生成动态封面时 Toolkit 页面一起卡死。
- 动态渲染增加独立进程和超时保护；超时或失败时会自动降级为同模板第一帧静态封面。
- 动态图片检测改为直接识别 PNG 动画块，避免上传前再次完整解码动态图。
- 上传后会回读当前封面并检测动画块，避免 Emby 静态化动态图时误报成功。

### 优化
- 动态封面改为目标尺寸直接渲染，不再每帧生成 1920x1080 后缩图。
- 背景、海报缩放、圆角、阴影等静态素材会预处理复用，每帧只合成移动部分。
- 默认动态模板从 200 帧调整为 32 帧，帧间隔调整为 80ms，更适合 NAS 环境。
""",
    },
    {
        "version": "v7.0.8",
        "published_at": "2026-07-09T00:20:00+08:00",
        "url": "https://github.com/cosmotown/emby-toolkit/tree/v7.0.8",
        "changelog": """## 动态封面轻量化

### 修复
- 修复动态 ChillPoster 在 960 宽度下生成任务卡死的问题。
- 动态 APNG 生成改为轻量上限：最大宽度 640、最大 45 帧、最多 2 个渲染线程。
- 后端会强制裁剪模板中过高的帧数配置，避免内置模板的 200 帧配置拖死任务。

### 调整
- 动态封面默认宽度从 960 改为 480。
- 前端动态宽度输入最大值改为 640，并提示 960 已知容易卡住。
""",
    },
    {
        "version": "v7.0.7",
        "published_at": "2026-07-08T23:05:00+08:00",
        "url": "https://github.com/cosmotown/emby-toolkit/tree/v7.0.7",
        "changelog": """## 动态封面生成热修复

### 修复
- 修复 `v7.0.6` 动态 ChillPoster 生成后可能卡死的问题。
- 移除后台 APNG 转 GIF 备用流程，避免大动态图二次转码拖死任务。
- 动态封面校验不再下载整张当前封面，只通过 Emby 图片 Tag 判断是否替换成功。

### 调整
- 动态封面现在只尝试 APNG 原样上传；Emby 未确认替换时直接降级静态 JPEG。
- 动态是否播放以 Emby 前端实际显示为准，后台不再做昂贵的多帧回读校验。
""",
    },
    {
        "version": "v7.0.6",
        "published_at": "2026-07-08T22:30:00+08:00",
        "url": "https://github.com/cosmotown/emby-toolkit/tree/v7.0.6",
        "changelog": """## 封面上传闭环修复

### 修复
- 动态 ChillPoster 封面不再提前强制压成静态 JPEG；上传时会先尝试保留 APNG 动画。
- 如果 Emby 未确认 APNG 动画可用，会自动尝试动态 GIF，最后才降级为静态 JPEG 兼容版。
- 封面上传后会回读 Emby 当前 Primary 图片 Tag 和图片内容，避免后台显示成功但实际未生效。

### 优化
- 上传成功后轻量请求 Emby 刷新当前项目图片缓存，减少页面继续显示旧图的概率。
- 日志会记录上传格式、Tag 变化和动态帧校验结果，后续排查封面问题不用再猜。
""",
    },
    {
        "version": "v7.0.5",
        "published_at": "2026-07-01T19:20:00+08:00",
        "url": "https://github.com/cosmotown/emby-toolkit/tree/v7.0.5",
        "changelog": """## ChillPoster 字体修复

### 修复
- 补齐 ChillPoster 原镜像内置字体，修复模板标题显示为方块的问题。
- ChillPoster 现在优先使用模板指定字体；模板字体不存在时再回退到用户本地字体或 toolkit 默认字体。

### 说明
- 本版未加入模板编辑器，只先修复现有模板生成效果。
""",
    },
    {
        "version": "v7.0.4",
        "published_at": "2026-07-01T18:45:00+08:00",
        "url": "https://github.com/cosmotown/emby-toolkit/tree/v7.0.4",
        "changelog": """## ChillPoster 标题热修复

### 修复
- 修复 ChillPoster 模板在英文标题为空时继续显示模板默认 `Western Movies` 的问题。
- ChillPoster 现在只使用封面标题配置传入的中文/英文标题；英文标题为空时保持为空，不再回退模板默认值。
""",
    },
    {
        "version": "v7.0.3",
        "published_at": "2026-07-01T06:05:00+08:00",
        "url": "https://github.com/cosmotown/emby-toolkit/tree/v7.0.3",
        "changelog": """## 封面生成热修复

### 修复
- 修复 `v7.0.2` 中原生单图、多图封面生成后后台显示成功但 Emby 实际封面不更新的问题。
- 统一封面上传前的数据处理，兼容原生模板返回的 base64 图片和 ChillPoster 返回的图片 bytes。
- 修复 ChillPoster 模板在标题配置为空时显示模板默认标题的问题，现在会回退显示媒体库名称。

### 说明
- 自建合集封面继续复用同一套封面生成配置，但需要通过自建合集页面的“生成所有封面”或刷新自建合集任务触发。
""",
    },
    {
        "version": "v7.0.2",
        "published_at": "2026-07-01T02:35:00+08:00",
        "url": "https://github.com/cosmotown/emby-toolkit/tree/v7.0.2",
        "changelog": """## 启动失败热修复

### 修复
- 修复 `v7.0.1` 镜像启动时报 `ModuleNotFoundError: No module named 'release_notes'` 的问题。
- 将本分支更新日志文件加入 Docker 镜像构建拷贝列表。
""",
    },
    {
        "version": "v7.0.1",
        "published_at": "2026-07-01T02:10:00+08:00",
        "url": "https://github.com/cosmotown/emby-toolkit/tree/v7.0.1",
        "changelog": """## 封面与更新链路修复

### 修复
- 修复封面生成后台显示成功但 Emby 实际封面不变化的问题：上传给 Emby 的图片内容改为 base64 编码，并补充 Token 请求头。
- 修正一键更新默认镜像地址为 `tzyzero186/emby-toolkit:latest`，避免误拉原项目镜像。

### 优化
- 定时更新封面入口移到封面生成页的基础设置区，更容易找到。
- 更新日志页面改为读取本分支维护的记录，不再显示原项目 10.x 更新内容。
""",
    },
    {
        "version": "v7.0.0",
        "published_at": "2026-07-01T01:20:00+08:00",
        "url": "https://github.com/cosmotown/emby-toolkit/tree/v7.0.0",
        "changelog": """## 基于原 6.8.9 分支定制

### 新增
- 加入 ChillPoster 封面模板。
- 新增封面定时更新选项，可按 CRON 固定时间生成原生媒体库封面。
- Webhook 增加 Token 校验，降低公网误触发风险。

### 修复
- 修复 ChillPoster 动态 PNG/APNG 上传到 Emby 后页面显示不更新的问题。Emby 实际上传使用兼容 JPEG，原始动态图仍可另存。
- 修复前端内部版本号仍显示 6.8.9 的问题。

### 调整
- 移除已失效的 NULLBR 模块，避免继续显示和调用不可用资源库。
- 保持 Docker 镜像双标签发布：固定版本号和 latest。
""",
    },
]
