# Canvasphere — yjs 协同服务镜像
# 由 infra 验证 worker 于 2026-11-25 补写：docker-compose.yml 的 yjs 服务
# 原先用 node:20-alpine + corepack + pnpm dlx 起 y-websocket，实测发现
# y-websocket 的 bin/server.cjs 只读 HOST/PORT 环境变量，不解析
# --port/--host 命令行参数（传参后仍绑 localhost:1234，容器外不可达），
# 且 pnpm dlx 每次启动都要联网拉包。改为构建期锁定版本、环境变量定地址。
# 本机无 docker，未实际构建验证，见 README 未实现清单。
FROM node:20-alpine

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev

# server.cjs 默认 HOST=localhost PORT=1234；容器里必须绑 0.0.0.0 才能
# 被 docker 的 1234:1234 端口映射访问。compose 里也显式传了这两个变量。
ENV HOST=0.0.0.0 PORT=1234

EXPOSE 1234
CMD ["node", "node_modules/y-websocket/bin/server.cjs"]
