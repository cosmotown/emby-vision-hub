# release_notes.py

CUSTOM_RELEASES = [
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
