"""Phase P6 tests: multi-turn ReAct tool-calling + mid-loop approval resume,
Studio SSE streaming, multi-track FFmpeg filter_complex timeline export,
keepalive sandbox container reuse & cleanup, Prometheus /metrics + JSON logging,
and Yjs JWT WebSocket room auth verification.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app import asset_tools, auth, observability, sandbox, studio_agents
from app.studio_agents import ModelTurnResult


def _auth_project_canvas(strict_client, email="p6@example.com"):
    strict_client.post("/api/auth/email/send-code", json={"email": email, "purpose": "register"})
    code = auth.get_last_sent_code(email, "register")
    reg = strict_client.post(
        "/api/auth/register",
        json={"email": email, "password": "Password123!", "code": code, "display_name": "P6 Owner"},
    )
    assert reg.status_code == 201, reg.text
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    proj = strict_client.post("/api/projects", headers=headers, json={"name": "P6 Studio"})
    assert proj.status_code in (200, 201), proj.text
    pid = proj.json()["id"]
    cid = proj.json()["default_canvas_id"]
    return headers, pid, cid


def test_react_tool_loop_midloop_approval_and_resume(strict_client, monkeypatch):
    headers, pid, cid = _auth_project_canvas(strict_client, "p6-react@example.com")

    # Seed one canvas node so read_canvas returns real data
    obj = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects",
        headers=headers,
        json={"type": "text", "name": "Script", "text": "赛博朋克雨夜分镜脚本"},
    )
    assert obj.status_code in (200, 201), obj.text

    turns = iter([
        ModelTurnResult(
            content="",
            tool_calls=[{"id": "tc-1", "name": "read_canvas", "args": {}}],
            mock=False,
        ),
        ModelTurnResult(
            content="",
            tool_calls=[{
                "id": "tc-2",
                "name": "generate_image",
                "args": {"prompt": "Cyberpunk rainy Tokyo street", "model_key": "gpt-image-2.6-flare-ox"},
            }],
            mock=False,
        ),
        ModelTurnResult(
            content="已完成画布读取并生成赛博朋克雨夜概念图。",
            tool_calls=[],
            mock=False,
        ),
    ])

    def _fake_invoke(**kwargs):
        return next(turns)

    monkeypatch.setattr(studio_agents, "invoke_tool_calling_model", _fake_invoke)

    # Turn 1 (read_canvas: auto) runs immediately; Turn 2 (generate_image: always) suspends run
    r1 = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/studio/chat",
        headers=headers,
        json={
            "prompt": "读取画布脚本并帮我生成概念图",
            "agent": "xiaoa",
            "enable_tool_loop": True,
        },
    )
    assert r1.status_code == 201, r1.text
    run1 = r1.json()["run"]
    assert run1["status"] == "waiting_approval"
    assert run1["pending_tool"]["mode"] == "react_tool_call"
    assert run1["pending_tool"]["tool"] == "generate_image"
    assert len(run1["pending_tool"]["tool_trace"]) == 1
    assert run1["pending_tool"]["tool_trace"][0]["name"] == "read_canvas"
    assert run1["pending_tool"]["tool_trace"][0]["output"]["count"] == 1

    # Approve suspended tool call -> executes generate_image -> Turn 3 finalizes run
    r2 = strict_client.post(
        f"/api/projects/{pid}/studio/runs/{run1['id']}/approve",
        headers=headers,
        json={"expected_fence": run1["generation_fence"]},
    )
    assert r2.status_code == 200, r2.text
    run2 = r2.json()
    assert run2["status"] == "done"
    assert "赛博朋克雨夜概念图" in run2["result"]["text"]
    assert len(run2["result"]["tool_trace"]) == 2
    assert run2["result"]["tool_trace"][1]["name"] == "generate_image"
    assert run2["result"]["tool_trace"][1]["approved"] is True
    assert run2["result"]["created_object_id"] is not None


def test_studio_chat_sse_streaming_endpoint(strict_client):
    headers, pid, cid = _auth_project_canvas(strict_client, "p6-sse@example.com")
    strict_client.post(f"/api/projects/{pid}/approval-policy", json={"tool": "add_node", "tier": "auto"}, headers=headers)

    resp = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/studio/chat/stream",
        headers=headers,
        json={"prompt": "帮我写一段简短开场白", "agent": "orange"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers.get("x-accel-buffering") == "no"
    body = resp.text
    assert "event: session\n" in body
    assert "event: plan\n" in body
    assert "event: token\n" in body
    assert "event: done\n" in body


def test_multitrack_ffmpeg_filter_complex_and_export(strict_client):
    # Verify atempo decomposition for extreme speeds
    assert asset_tools.build_atempo_chain(0.25) == "atempo=0.5,atempo=0.5000"
    assert asset_tools.build_atempo_chain(4.0) == "atempo=2.0,atempo=2.0000"

    normalized = asset_tools._validate_timeline_draft(
        {
            "title": "MultiTrack Cut",
            "fps": 30,
            "clips": [
                {
                    "track": "video",
                    "source_object_id": "obj_v1",
                    "start_ms": 0,
                    "duration_ms": 4000,
                    "speed": 2.0,
                    "fade_in_ms": 200,
                    "fade_out_ms": 300,
                },
                {
                    "track": "audio",
                    "source_object_id": "obj_a1",
                    "start_ms": 0,
                    "duration_ms": 3000,
                    "speed": 1.5,
                    "preserve_pitch": True,
                    "gain_db": -3.0,
                    "fade_in_ms": 100,
                },
                {
                    "track": "audio",
                    "source_object_id": "obj_a2",
                    "start_ms": 500,
                    "duration_ms": 2000,
                    "mute": True,
                },
            ],
        },
        "mp4",
    )
    cmd, fc = asset_tools.build_ffmpeg_timeline_command(
        "ffmpeg", normalized, [None, None, None], Path("out.mp4"), "mp4", threads=2
    )
    assert "-threads" in cmd and "2" in cmd
    assert "setpts=(PTS-STARTPTS)/2.0000" in fc
    assert "fade=t=in:st=0:d=0.200" in fc
    assert "atempo=1.5000" in fc
    assert "volume=-3.00dB" in fc
    assert "volume=0" in fc
    assert "adelay=500|500" in fc
    assert "amix=inputs=2:duration=longest:normalize=0[aout]" in fc

    # Run via endpoint and verify filter_complex is persisted in job output
    headers, pid, cid = _auth_project_canvas(strict_client, "p6-ffmpeg@example.com")
    v_obj = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects",
        headers=headers,
        json={"type": "video", "name": "Clip V1"},
    ).json()["id"]
    a_obj = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/objects",
        headers=headers,
        json={"type": "audio", "name": "Clip A1"},
    ).json()["id"]

    exp = strict_client.post(
        f"/api/projects/{pid}/canvases/{cid}/asset-tools/run",
        headers=headers,
        json={
            "tool_name": "timeline.export",
            "payload": {
                "output_format": "mp4",
                "draft": {
                    "title": "Final Cut",
                    "clips": [
                        {"track": "video", "source_object_id": v_obj, "start_ms": 0, "duration_ms": 2000, "speed": 1.0},
                        {"track": "audio", "source_object_id": a_obj, "start_ms": 250, "duration_ms": 2000, "gain_db": -2.5},
                    ],
                },
            },
        },
    )
    assert exp.status_code == 201, exp.text
    out = exp.json()["output"]
    assert "filter_complex" in out
    assert "adelay=250|250" in out["filter_complex"]
    assert "volume=-2.50dB" in out["filter_complex"]


def test_keepalive_sandbox_container_reuse_and_destroy(strict_client, monkeypatch):
    headers, pid, _cid = _auth_project_canvas(strict_client, "p6-sbx@example.com")

    # Enable sandbox on project
    en = strict_client.patch(
        f"/api/projects/{pid}/sandbox",
        headers=headers,
        json={"enabled": True, "spec_name": "2核4G", "spend_cap_credits": 50},
    )
    assert en.status_code == 200, en.text

    started: list[tuple[str, str | None]] = []
    executed: list[tuple[str, str]] = []
    destroyed: list[str] = []

    def _fake_ensure(session_id: str, workspace_dir: str | None = None) -> str:
        started.append((session_id, workspace_dir))
        return f"cid-{session_id}"

    def _fake_exec(container_id: str, code: str, timeout: int = 30):
        executed.append((container_id, code))
        return {
            "sandbox": "local-docker-keepalive",
            "container_id": container_id,
            "stdout": "hello\n",
            "stderr": "",
            "exit_code": 0,
            "error": None,
        }

    def _fake_destroy(container_id: str | None) -> bool:
        if container_id:
            destroyed.append(container_id)
        return True

    monkeypatch.setattr(sandbox, "ensure_keepalive_container", _fake_ensure)
    monkeypatch.setattr(sandbox, "exec_in_container", _fake_exec)
    monkeypatch.setattr(sandbox, "destroy_container", _fake_destroy)

    # Execute twice -> keepalive container started ONCE, docker exec called TWICE with same container_id
    r1 = strict_client.post(
        f"/api/projects/{pid}/sandbox/execute",
        headers=headers,
        json={"code": "print('run1')", "simulated_duration_seconds": 10},
    )
    assert r1.status_code == 200, r1.text
    r2 = strict_client.post(
        f"/api/projects/{pid}/sandbox/execute",
        headers=headers,
        json={"code": "print('run2')", "simulated_duration_seconds": 10},
    )
    assert r2.status_code == 200, r2.text

    assert len(started) == 1
    assert started[0][1] is not None  # workspace dir passed for mount
    assert len(executed) == 2
    assert executed[0][0] == executed[1][0]
    assert r1.json()["session_id"] == r2.json()["session_id"]

    # Stopping sandbox destroys the keepalive container
    stop = strict_client.post(f"/api/projects/{pid}/sandbox/stop", headers=headers)
    assert stop.status_code == 200, stop.text
    assert destroyed == [executed[0][0]]


def test_observability_metrics_json_logging_and_yjs_auth(strict_client):
    # Hit health endpoint so metrics middleware records an observation
    h = strict_client.get("/health")
    assert h.status_code == 200

    m = strict_client.get("/metrics")
    assert m.status_code == 200
    assert m.headers["content-type"].startswith("text/plain")
    assert 'canvasphere_http_requests_total{method="GET",path="/health",status="200"} 1' in m.text
    assert 'canvasphere_http_request_duration_seconds_count{method="GET",path="/health"} 1' in m.text

    # Verify structured JSON log formatter outputs parseable JSON
    formatter = observability.JsonLogFormatter()
    rec = logging.LogRecord("canvasphere.test", logging.INFO, __file__, 1, "hello %s", ("p6",), None)
    parsed = json.loads(formatter.format(rec))
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "canvasphere.test"
    assert parsed["message"] == "hello p6"
    assert "ts" in parsed

    # Verify Yjs JWT auth server script & nginx HTTPS template exist on repo
    repo_root = Path(__file__).resolve().parents[2]
    yjs_script = (repo_root / "yjs" / "server.cjs").read_text(encoding="utf-8")
    assert "verifyHs256Jwt" in yjs_script
    assert "YJS_REQUIRE_AUTH" in yjs_script
    nginx_conf = (repo_root / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    assert "443 ssl http2" in nginx_conf
    assert "metrics" in nginx_conf
    assert "studio/chat/stream" in nginx_conf
