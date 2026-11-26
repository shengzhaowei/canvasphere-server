"""Sandbox: local Docker is the default; E2B only when E2B_API_KEY exists.

2026-11-26：E2B 注册要求绑美国银行卡，jacx 没有，改走本地 Docker。
api 容器内装 docker 客户端 + 挂载宿主机 /var/run/docker.sock，
沙盒容器在宿主机上起（兄弟容器），与 api/db/web 共用这台 4 核 8G，
不用再买服务器。每个沙盒限 512m 内存 / 0.5 CPU / 128 进程、无网络。

Every result labels its backend explicitly:
  sandbox: "e2b" | "local-docker" | "unavailable"
No key is ever hard-coded here; E2B_API_KEY comes from the environment.
Local Docker runs --network none, --rm, python:3.12-slim.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def run_local_docker(code: str, timeout: int = 30) -> dict[str, Any]:
    """Run `code` in a throwaway python:3.12-slim container, no network.

    Resource limits keep one sandbox from eating the shared 4c8g host:
    512m RAM, 0.5 CPU, 128 processes. First run pulls python:3.12-slim
    (~50MB) on the host, so deploy pre-pulls it (see docs/deploy-seoul.md).
    """
    if shutil.which("docker") is None:
        return {"sandbox": "unavailable", "stdout": "", "stderr": "",
                "exit_code": None,
                "error": "本地沙盒不可用：未安装 docker"}
    try:
        from app.config import get_settings
        s = get_settings()
        proc = subprocess.run(
            ["docker", "run", "--rm", "--network", "none",
             "--memory", s.sandbox_docker_memory, "--cpus", s.sandbox_docker_cpus, "--pids-limit", "128",
             "python:3.12-slim", "python", "-c", code],
            capture_output=True, text=True, timeout=timeout)
        return {"sandbox": "local-docker",
                "stdout": proc.stdout, "stderr": proc.stderr,
                "exit_code": proc.returncode, "error": None}
    except subprocess.TimeoutExpired:
        return {"sandbox": "local-docker", "stdout": "", "stderr": "",
                "exit_code": None, "error": f"timed out after {timeout}s"}
    except Exception as exc:  # noqa: BLE001
        return {"sandbox": "unavailable", "stdout": "", "stderr": "",
                "exit_code": None, "error": f"本地沙盒执行失败：{exc}"}


def run_e2b(code: str, timeout: int = 30) -> dict[str, Any]:
    """Run `code` with the E2B code-interpreter SDK (optional dependency)."""
    try:
        from e2b_code_interpreter import Sandbox
    except ImportError:
        return {"sandbox": "unavailable", "stdout": "", "stderr": "",
                "exit_code": None,
                "error": "E2B SDK 未安装（e2b-code-interpreter）"}
    try:
        with Sandbox(timeout=timeout) as sbx:
            execution = sbx.run_code(code)
        stdout = "\n".join(getattr(o, "text", "") or ""
                           for o in (execution.logs.stdout or []))
        stderr = "\n".join(getattr(o, "text", "") or ""
                           for o in (execution.logs.stderr or []))
        err = execution.error
        return {"sandbox": "e2b", "stdout": stdout, "stderr": stderr,
                "exit_code": 0 if err is None else 1,
                "error": (getattr(err, "value", None) or str(err))
                         if err is not None else None}
    except Exception as exc:  # noqa: BLE001
        logger.warning("E2B run failed: %s", exc)
        return {"sandbox": "unavailable", "stdout": "", "stderr": "",
                "exit_code": None, "error": f"E2B 执行失败：{exc}"}


def run_code(code: str, timeout: int = 30) -> dict[str, Any]:
    """Dispatch: E2B if E2B_API_KEY is set, else local Docker fallback."""
    if not code or not code.strip():
        return {"sandbox": "unavailable", "stdout": "", "stderr": "",
                "exit_code": None, "error": "code must not be empty"}
    if os.environ.get("E2B_API_KEY"):
        return run_e2b(code, timeout=timeout)
    logger.info("no E2B_API_KEY; falling back to local Docker sandbox")
    return run_local_docker(code, timeout=timeout)
