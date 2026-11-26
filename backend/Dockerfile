# Canvasphere — 后端镜像
# 由协调员补写：docker-compose.yml 的 api 服务 build: ./backend 需要它，
# 本机无 docker，未能实际构建验证，见 README 未实现清单。
FROM python:3.12-slim

# curl 供 docker-compose 的 api healthcheck（curl -f http://localhost:8000/health）使用
# docker 客户端：沙盒改走本地 Docker（2026-11-26，E2B 要求绑美国银行卡，
# jacx 没有，改用宿主机 Docker）。只装静态客户端二进制，不装 daemon；
# 运行时通过挂载 /var/run/docker.sock 与宿主机 daemon 通信。
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.3.1.tgz -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && docker --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
