import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.permissions import (  # noqa: E402
    check_permission, require_approval, can_write, assert_permission,
)


def test_viewer_read_only():
    assert check_permission("viewer", "read_canvas") is True
    assert check_permission("viewer", "list_project_files") is True
    assert check_permission("viewer", "add_node") is False
    assert check_permission("viewer", "update_node") is False
    assert check_permission("viewer", "delegate_task") is False
    assert check_permission("viewer", "run_code") is False
    assert can_write("viewer") is False


def test_owner_editor_full_allowlist():
    for role in ("owner", "editor"):
        for tool in ("read_canvas", "add_node", "update_node",
                     "list_project_files", "delegate_task",
                     "run_code", "generate_image", "generate_video"):
            assert check_permission(role, tool) is True, (role, tool)
        assert can_write(role) is True


def test_destructive_tools_require_approval():
    # ALWAYS tier: always approval. SESSION tier: approval until a
    # session grant exists (no project_id -> no grant -> approval).
    for tool in ("run_code", "generate_image", "generate_video"):
        assert require_approval(tool) is True
    for tool in ("read_canvas", "list_project_files"):
        assert require_approval(tool) is False
    for tool in ("add_node", "update_node", "delegate_task"):
        assert require_approval(tool) is True  # session tier, no grant yet


def test_tools_outside_allowlist_rejected():
    assert check_permission("owner", "delete_project") is False
    assert check_permission("owner", "exec_shell") is False
    assert check_permission("admin", "read_canvas") is False
    assert check_permission("viewer", "delete_project") is False
    with pytest.raises(PermissionError):
        assert_permission("viewer", "add_node")
    with pytest.raises(PermissionError):
        assert_permission("owner", "delete_project")
    with pytest.raises(PermissionError):
        assert_permission("admin", "read_canvas")
