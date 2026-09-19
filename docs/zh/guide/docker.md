# Docker 部署

推荐使用 `docker-compose.yml` 方式部署，以下示例与仓库 README 一致，并补充关键说明。

> 兼容说明：新部署使用 `emby-vision-hub` 命名。旧的 `tzyzero186/emby-toolkit` 镜像标签在 7.2.0 继续同步发布，老用户可以保持现有 Compose、`/config` 和 PostgreSQL 数据原地升级。

## 目录准备

```bash
mkdir -p /path/emby-vision-hub
```

## 示例 Compose

```yaml
services:
  emby-vision-hub:
    image: tzyzero186/emby-vision-hub:latest
    container_name: emby-vision-hub
    network_mode: bridge
    ports:
      - "5257:5257"  # Web 控制台
      - "8097:8097"  # 反向代理/虚拟库端口
    volumes:
      - /path/emby-vision-hub:/config
      - /path/media:/media
      - /path/tmdb:/tmdb
    environment:
      - APP_DATA_DIR=/config
      - TZ=Asia/Shanghai
      - PUID=1000
      - PGID=1000
      - UMASK=022
      - DB_HOST=172.17.0.1
      - DB_PORT=5433
      - DB_USER=evh
      - DB_PASSWORD=请替换为强密码
      - DB_NAME=evh
      - CONTAINER_NAME=emby-vision-hub
      - DOCKER_IMAGE_NAME=tzyzero186/emby-vision-hub:latest
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy

  db:
    image: postgres:18
    container_name: emby-vision-hub-db
    restart: unless-stopped
    network_mode: bridge
    volumes:
      - postgres_data:/var/lib/postgresql
    environment:
      - POSTGRES_USER=evh
      - POSTGRES_PASSWORD=请替换为强密码
      - POSTGRES_DB=evh
    ports:
      - "5433:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U evh -d evh"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  postgres_data:
```

## 端口说明

- `5257`：主 Web 控制台（API 与前端 UI）。
- `8097`：反向代理端口（虚拟库/合并视图）。

## 持久化目录

- `/config`：配置、日志、数据库连接信息等持久化数据。
- `/media`：媒体库目录（实时监控与增量处理）。

## 启动

```bash
docker-compose up -d
```

## 内建更新器

- 配置优先级：运行环境变量 > Web 页面保存配置 > 默认值。`CONTAINER_NAME` 应准确指向当前 EVH 容器，`DOCKER_IMAGE_NAME` 仅允许官方 `tzyzero186/emby-vision-hub` 仓库。
- 仅支持经过自身身份验证的独立 Docker；必须有本地持久 `/config`、Docker socket、有效 Docker Healthcheck。
- Compose（含 latest）、Portainer、1Panel、Swarm、Kubernetes、Nomad 请使用原管理器升级。不声称能识别没有 inspect 标记的所有第三方管理器。
- NFS/SMB/未知锁文件系统、tmpfs、特殊 volume options 和静态网络配置不支持自动替换。出现 `AMBIGUOUS` 时需通过原管理器核查恢复，不能重复点击绕过。
- 第一次升级到包含新更新器的版本必须用外部管理器；之后的升级才会使用本更新器。
- 独立 Docker 的内建更新要求 `/config` 为唯一可写持久化挂载，`/var/run/docker.sock` 为唯一可写 bind mount。Docker socket 具有宿主机管理权限；上方 Compose 示例不需要挂载。
- 更新成功必须同时满足：运行 image ID 等于目标、`APP_VERSION` 等于正式 Release、容器 healthy、关键配置指纹一致。失败时会尝试恢复旧容器。
