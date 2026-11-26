# Canvasphere Server

Canvasphere 的后端仓库：FastAPI + LangGraph 多智能体、Yjs 协同服务、Postgres。

前端不在这里，住在 [`canvasphere`](https://github.com/shengzhaowei/canvasphere) 仓库（React + Vite，Vercel 部署）。

## 结构

- `backend/` — FastAPI + LangGraph：Supervisor 调度 + 故事/视觉/视频/声音 4 个子智能体、会话主机（检查点恢复）、权限边界、审批流、Skill 系统、本地 Docker 沙盒、MCP Server（FastMCP，挂在 `/mcp`）
- `yjs/` — y-websocket 协同服务（端口 1234）
- `skills/` — 技能定义（`skills/*/SKILL.md`）
- `docker-compose.yml` — api + yjs + db（+ 可选 redis）
- `mcp.json` — MCP 接入示例

## 本地开发

```bash
cp .env.example .env   # 填真实 LLM Key 等值
docker compose up -d --build
curl http://localhost:8000/health
```

后端测试：`cd backend && python -m pytest tests -q`（44 个测试）。

## 部署

推代码到 `main` → GitHub Actions 自动跑后端测试 → 通过后自动部署到首尔服务器（阿里云轻量，`8.220.194.80`，目录 `/opt/canvasphere`，与旧版同一目录，数据库卷得以保留）。设置见 `docs/cicd.md`。

## 环境变量

见 `.env.example`。注意两点：

- `.env` 不在包里、不进仓库，部署不会覆盖服务器上的 `.env`。
- 前端换到 Vercel 后，要把 Vercel 域名加进服务器 `.env` 的 `CORS_ORIGINS`，否则浏览器跨域调不到后端。
