# Canvasphere — yjs 协同服务镜像（内置 HS256 JWT 房间鉴权）
FROM node:20-alpine

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY server.cjs ./

ENV HOST=0.0.0.0 PORT=1234 YJS_REQUIRE_AUTH=true
EXPOSE 1234
CMD ["node", "server.cjs"]
