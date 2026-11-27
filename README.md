# Canvasphere Server

Canvasphere（Pecan Studio 1:1 后端架构）生产级服务端仓库：FastAPI + SQLAlchemy 2.0/Alembic + LangGraph 多智能体 ReAct/SSE + Redis/ARQ 异步 Worker + Yjs JWT 协同服务 + Postgres 16 + Nginx 反向代理。

前端仓库位于 [`canvasphere`](https://github.com/shengzhaowei/canvasphere)（React + Vite）。

## 核心能力（P0–P7）

- **认证与账户安全**：邮箱+密码、双 Token 轮换与全端登出、忘记密码重置、GitHub/Google/Linux DO OAuth、滑动窗口限流与严格 JWT 鉴权。
- **无限画布与实时协同**：10 类画布对象与连线、乐观锁版本冲突 `409`、节点编辑租约、Redis Pub/Sub SSE 实时事件流、相机协同与 Yjs HS256 JWT 房间鉴权。
- **多模态生成与双池计费**：64+ 费率卡、Decimal 双池钱包（`bonus` / `cash`）+ Postgres `FOR UPDATE` 行锁防双花、兑换码、6 大厂商协议适配器（OpenAI/Ark/Midjourney/Seedance/MiniMax/Suno）、Fernet 加密 BYOK 运行时路由与幂等 Webhook。
- **5 智能体 Studio 编排**：`pecan` / `orange` / `xiaoa` / `blueberry` / `yueyue`、画布 DAG 上下文检索、Token 自动压缩落盘、多轮 `bind_tools` ReAct 循环（支持中途分级审批挂起与恢复）及 `POST .../studio/chat/stream` SSE 流式输出。
- **素材工具、沙盒与技能/MCP**：多轨 FFmpeg `filter_complex` 时间线导出与九宫格切图、Keepalive Docker 沙盒容器复用与宿主机并发/内存硬护栏、技能 `.zip` 上传（Zip Slip 防护）、双入口 OAuth 2.0 DCR MCP（`/mcp/canvas/` & `/mcp/pecan/`）与平台 Admin 管理 API。
- **生产可观测性与运维**：结构化 JSON 日志、零依赖 Prometheus `GET /metrics`、深度就绪探针 `GET /health/ready`、运维初始化 CLI（`python -m app.cli`）与 7 天滚动备份脚本。

## 仓库结构

- `backend/` — FastAPI 应用、Alembic 迁移（`0001`–`0005`）、ARQ Worker、运维 CLI 与全量自动化测试
- `yjs/` — 带 HS256 JWT 房间鉴权的 `y-websocket` 协同服务（端口 1234）
- `nginx/` — 4C8G 生产反代配置（SSE 零缓冲、双 MCP、Yjs WS、`/metrics` 与 HTTPS 443 模板）
- `skills/` — 内置技能定义（`skills/*/SKILL.md`）
- `scripts/` — 部署打包（`package.sh`）与 Postgres 滚动备份（`backup-pg.sh`）
- `docker-compose.yml` — 面向 4 vCPU / 8 GiB RAM 调优的 6 服务编排（`nginx` + `api` + `worker` + `yjs` + `db` + `redis`）

## 本地开发与测试

```bash
cp .env.example .env   # 替换为真实密钥与模型配置
docker compose up -d --build
curl http://localhost:8000/health
curl http://localhost:8000/health/ready
```

零外部依赖全量测试（CI 同款，SQLite 内存库 + 内存回退）：

```bash
cd backend && python -m pytest tests -q
```

## 生产初始化运维 CLI（容器内执行）

```bash
# 1. 创建或提权首位平台管理员
docker compose exec api python -m app.cli bootstrap-admin \
  --email admin@yourdomain.com --password 'StrongPassword123!'

# 2. 生成积分兑换码
docker compose exec api python -m app.cli create-redeem-code \
  --code LAUNCH500 --credits 500 --max-uses 100

# 3. 为指定用户开通 BYOK 权限
docker compose exec api python -m app.cli grant-byok \
  --email user@yourdomain.com --max-channels 10

# 4. 生产环境配置体检
docker compose exec api python -m app.cli check-env --strict
```

## 自动部署（CI/CD）

推代码到 `main` → GitHub Actions 自动跑全量后端测试 → 通过后自动部署到首尔 4C8G 服务器（`8.220.194.80:/opt/canvasphere`，保留 `pgdata`/`redis_data`/`storage_data` 数据卷与服务器 `.env`）。详细操作见 [docs/deploy-seoul.md](docs/deploy-seoul.md) 与 [docs/cicd.md](docs/cicd.md)。
