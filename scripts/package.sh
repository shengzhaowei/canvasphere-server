#!/usr/bin/env bash
# 本地打包：跑后端测试 + 打出与 CI 一致的部署包。
# 用法：在仓库根目录 ./scripts/package.sh
# 产物：../canvasphere.tar.gz（不含 .env、.venv、__pycache__）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== backend tests =="
(cd backend && .venv/bin/python -m pytest tests -q)

echo "== pack =="
tar -czf ../canvasphere.tar.gz \
  --exclude=canvasphere.tar.gz \
  --exclude=.venv \
  --exclude=backend/__pycache__ \
  --exclude=.pytest_cache --exclude=.env --exclude=.git \
  .
ls -lh ../canvasphere.tar.gz
