# 独立项目说明

EVH 自 7.2.0 起以 `cosmotown/emby-vision-hub` 作为独立项目仓库继续维护。项目保留完整 Git 历史并遵循 AGPL-3.0 许可证，后续功能、界面和发布节奏由 EVH 独立演进。

## 项目定位

EVH 面向 Emby 媒体库管理与自动化场景，集中提供 STRM 入库协调、元数据增强、智能订阅、媒体整理、合集与虚拟库、封面生成、任务调度、用户权限和运行诊断。

项目可以与 Emby、MoviePilot、TMDb、豆瓣及本地元数据服务协同，但不是这些第三方产品的官方组成部分。

## 兼容性边界

- UI、项目文档、版本检查和后续发布地址使用 EVH 与新仓库名称。
- Docker 镜像地址、容器名、配置目录和数据库名暂时保留既有值，避免现有部署升级时迁移数据。
- Git 标签保留全部历史版本；7.2.0 及后续版本由独立仓库发布。
- VitePress 文档随主仓库迁移，GitHub Pages 基础路径为 `/emby-vision-hub/`。

## 维护与反馈

- 源代码与发布记录：`https://github.com/cosmotown/emby-vision-hub`
- 问题反馈：`https://github.com/cosmotown/emby-vision-hub/issues`
- 使用文档：仓库内 `docs/zh/`
