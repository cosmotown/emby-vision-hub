# release_notes.py

CUSTOM_RELEASES = [
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
