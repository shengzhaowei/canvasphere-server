"""MCP over streamable-http at /mcp (same 5 tools as stdio).

Regression test: mounting FastMCP's http_app without wiring its
lifespan into FastAPI leaves the StreamableHTTPSessionManager task
group uninitialised and every /mcp request 500s. The TestClient must
be used as a context manager so lifespan runs.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import store  # noqa: E402
from app.main import app  # noqa: E402

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1.0"}}}

HEADERS = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}


def _sse_payloads(response):
    """Parse text/event-stream body into JSON-RPC payload dicts."""
    out = []
    for line in response.text.splitlines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:].strip()))
    return out


def _initialize(client):
    r = client.post("/mcp/", json=INIT, headers=HEADERS)
    assert r.status_code == 200, r.text[:500]
    payloads = _sse_payloads(r)
    assert payloads and payloads[0].get("result", {}).get("protocolVersion") == "2025-03-26"
    assert payloads[0]["result"]["serverInfo"]["name"] == "canvasphere"
    session = r.headers.get("mcp-session-id")
    assert session, "initialize must return an mcp-session-id"
    return session


def test_mcp_http_initialize_and_tools_list():
    store.reset_store()
    project = store.create_project("mcp-http-proj")
    try:
        with TestClient(app) as client:
            session = _initialize(client)
            headers = dict(HEADERS, **{"mcp-session-id": session})
            client.post("/mcp/", json={"jsonrpc": "2.0",
                                       "method": "notifications/initialized"},
                        headers=headers)
            r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 2,
                                           "method": "tools/list", "params": {}},
                            headers=headers)
            assert r.status_code == 200, r.text[:500]
            payloads = _sse_payloads(r)
            assert payloads and "result" in payloads[0]
            names = {t["name"] for t in payloads[0]["result"]["tools"]}
            assert names == {"read_canvas", "add_node", "update_node",
                             "list_project_files", "delegate_task"}
            r = client.post("/mcp/", json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "read_canvas",
                           "arguments": {"project_id": project["id"]}}},
                headers=headers)
            assert r.status_code == 200, r.text[:500]
            assert project["id"] in r.text
    finally:
        store.reset_store()
