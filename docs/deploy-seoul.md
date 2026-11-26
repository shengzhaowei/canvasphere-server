# Canvasphere 部署 — 阿里云轻量 韩国首尔

服务器已买好（2026-11-26 查到）：

- 实例：Ubuntu-cvgq，ID `8cdf45950de949a8ad4c07a31b94a68e`
- 地域：韩国（首尔），轻量应用服务器（不是 ECS，可以部署）
- 公网 IP：**8.220.194.80**，内网 172.19.0.157，运行中
- 配置：4 vCPU / 8 GiB / 70 GiB ESSD，Ubuntu 24.04，200 Mbps
- 到期：2027-11-26 23:59:59，自动续费关
- 防火墙已开：TCP 80、443、22，ICMP；部署时又加了 TCP 5173、8000、1234 —— **不要再开 5432 / 6379**

## 上机前准备（本地已做）

- 项目已改名 Canvasphere，目录 `~/workspace/canvasphere`
- `CanvasPanel.tsx` 已读 `VITE_YJS_URL`（无值 fallback `ws://localhost:1234`），构建期用 build arg 传
- 后端 44 passed，前端 tsc + build 通过

## 部署步骤（在服务器上，root）

```bash
# 1. 装 Docker
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker compose version

# 2. 传项目（任选其一）
# 方式 A：本地打包后 scp
#   本地：cd ~/workspace && tar -czf canvasphere.tar.gz --exclude=node_modules --exclude=.venv --exclude=frontend/dist --exclude=backend/__pycache__ --exclude=.pytest_cache canvasphere
#   scp canvasphere.tar.gz root@8.220.194.80:/opt/
#   服务器：cd /opt && tar -xzf canvasphere.tar.gz && cd canvasphere
# 方式 B：阿里云 Workbench 远程连接后，用控制台的文件上传传 canvasphere.tar.gz 到 /opt，再解压

# 3. 配 .env（不要把真实 Key 发到聊天里，直接在服务器上填）
cp .env.example .env
# 编辑 .env，必填：
#   LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（你的中转或官方地址）
#   DATABASE_URL=postgresql://canvas:<强密码>@db:5432/canvas
#   VITE_API_URL=http://8.220.194.80:8000
#   YJS_URL=ws://8.220.194.80:1234
#   LLM_PRICE_INPUT / LLM_PRICE_OUTPUT 按中转实际价改
# 同步改 docker-compose.yml 里 db 的 POSTGRES_PASSWORD 为同一个强密码

# 4. 启动
docker compose up -d --build
docker compose ps
curl http://localhost:8000/health

# 5. 验证
# 浏览器打开 http://8.220.194.80:5173
# 创建项目 -> 发一个故事任务 -> 审批 -> 画布出现节点
# 另开一个浏览器标签再打开同一项目，改一个节点，看另一边是否同步（Yjs）
```

## 上线前必查

- `docker compose ps` 全部 healthy / up，`curl /health` 返回 checkpointer=postgres（不是 memory）
- 防火墙仍只有 80 / 443 / 22，不要开数据库端口
- 先用 IP 访问验证全链路；要上域名再配 Nginx + HTTPS（80/443 反代到 5173 / 8000 / 1234）
- 备份：`pgdata` 卷定期快照；.env 不要进 Git、不要发聊天
