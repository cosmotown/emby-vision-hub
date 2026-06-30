# release_notes.py

CUSTOM_RELEASES = [
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
