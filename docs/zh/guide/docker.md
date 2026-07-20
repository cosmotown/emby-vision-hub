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
      - /var/run/docker.sock:/var/run/docker.sock
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
