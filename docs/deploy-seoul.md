# Canvasphere 生产部署指南 — 阿里云轻量 韩国首尔（4 核 8G / 70G ESSD）

## 服务器规格

- 实例：Ubuntu-cvgq，地域：韩国（首尔），阿里云轻量应用服务器
- 公网 IP：**8.220.194.80**，内网 `172.19.0.157`
- 配置：**4 vCPU / 8 GiB RAM / 70 GiB ESSD**，Ubuntu 24.04，200 Mbps
- 放行端口：TCP `80`、`443`、`22`（及调试期 `8000`、`1234`）—— **严禁对外开放 `5432`（Postgres）与 `6379`（Redis）**

## 4C8G 容器资源预算（`docker-compose.yml`）

6 服务常驻内存合计约 **1.2–1.6 GiB**，总硬上限 **3.84 GiB**，为宿主机内核、FFmpeg 转码（限 2 线程/1 并发）与 Keepalive Docker 沙盒（限 2 并发 × 512 MiB）留足 ~3.4 GiB 安全余量：

| 服务 | 内存上限 | CPU 上限 | 说明 |
|---|---|---|---|
| `nginx` | 128 MiB | 0.25 | 80/443 反代、SSE 零缓冲、双 MCP、Yjs WS |
| `api` | 1 GiB | 1.0 | FastAPI（2 workers）+ 启动自动 `alembic upgrade head` |
| `worker` | 1 GiB | 1.0 | ARQ 异步队列 + 每分钟空闲沙盒回收 + 每日孤儿清理 |
| `yjs` | 256 MiB | 0.5 | `y-websocket` + 原生 HS256 JWT 房间鉴权 |
| `db` | 1.2 GiB | 1.0 | Postgres 16（`shared_buffers=256MB, max_connections=60`） |
| `redis` | 256 MiB | 0.25 | Redis 7（`maxmemory 256mb, noeviction, AOF`） |

## 首次上机部署步骤

```bash
# 1. 安装 Docker 与预拉取沙盒基础镜像
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker pull python:3.12-slim

# 2. 解压代码到 /opt/canvasphere 并配置 .env（绝不入库）
cd /opt/canvasphere
cp .env.example .env
# 必填/必改项：
#   JWT_SECRET=<openssl rand -hex 32>
#   POSTGRES_PASSWORD=<强密码>
#   DATABASE_URL=postgresql://canvas:<同上强密码>@db:5432/canvas
#   PROVIDER_WEBHOOK_SECRET=<openssl rand -hex 24>
#   LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
#   APP_BASE_URL=http://8.220.194.80（上域名后改 https://api.yourdomain.com）
#   CORS_ORIGINS=https://your-vercel-app.vercel.app,http://8.220.194.80:5173
#   PRODUCTION_STRICT=true

# 3. 启动 6 服务并等待健康检查通过
docker compose up -d --build
docker compose ps

# 4. 校验存活、深度就绪与 Prometheus 指标
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/health/ready
curl -fsS http://localhost:8000/metrics | head -n 10

# 5. 初始化首位平台管理员与首批兑换码
docker compose exec api python -m app.cli check-env --strict
docker compose exec api python -m app.cli bootstrap-admin \
  --email admin@yourdomain.com --password 'StrongPassword123!'
docker compose exec api python -m app.cli create-redeem-code \
  --code WELCOME500 --credits 500 --max-uses 100
```

## 70 GiB ESSD 每日自动备份

给 `scripts/backup-pg.sh` 赋权并加入 `crontab -e`（每日 UTC 03:15 压缩备份，自动清理 7 天前旧档）：

```bash
chmod +x /opt/canvasphere/scripts/backup-pg.sh
(crontab -l 2>/dev/null; echo "15 3 * * * /opt/canvasphere/scripts/backup-pg.sh >> /opt/canvasphere/backups/backup.log 2>&1") | crontab -
```

## 上线前核对清单

- `docker compose ps` 中 6 个服务全部 `Up` / `healthy`，且全部配置 `restart: unless-stopped`
- `curl /health` 返回 `checkpointer: "postgres"`、`db: "postgres"`、`redis: "redis"`
- `curl /health/ready` 返回 `200` 且 `warnings: []`
- 防火墙未暴露 `5432` 与 `6379`（`docker-compose.yml` 已绑定 `127.0.0.1`）
- 绑定域名后，参考 `nginx/nginx.conf` 底部模板启用 `443 ssl http2` 与 Certbot 自动续签
