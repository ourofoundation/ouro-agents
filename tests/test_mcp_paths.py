from pathlib import Path

from ouro_agents.mcp_paths import (
    remap_mcp_path_string,
    remap_mcp_value,
    wrap_mcp_tool_with_workspace_paths,
)


def test_remaps_container_absolute_path(tmp_path: Path):
    out = remap_mcp_path_string(
        "/workspace/scratch/out.cif",
        workspace_root=tmp_path,
        workspace_mount="/workspace",
    )
    assert out == str((tmp_path / "scratch" / "out.cif").resolve())


def test_remaps_relative_file_path_key(tmp_path: Path):
    out = remap_mcp_path_string(
        "drafts/invite.ics",
        workspace_root=tmp_path,
        workspace_mount="/workspace",
        key="filePath",
    )
    assert out == str((tmp_path / "drafts" / "invite.ics").resolve())


def test_leaves_relative_path_without_file_key(tmp_path: Path):
    assert (
        remap_mcp_path_string(
            "drafts/invite.ics",
            workspace_root=tmp_path,
            workspace_mount="/workspace",
            key="subject",
        )
        == "drafts/invite.ics"
    )


def test_leaves_urls_and_unrelated_absolute_paths(tmp_path: Path):
    url = "https://example.com/report.pdf"
    assert (
        remap_mcp_path_string(
            url,
            workspace_root=tmp_path,
            workspace_mount="/workspace",
            key="filePath",
        )
        == url
    )
    assert (
        remap_mcp_path_string(
            "/tmp/outside.cif",
            workspace_root=tmp_path,
            workspace_mount="/workspace",
            key="filePath",
        )
        == "/tmp/outside.cif"
    )


def test_escape_via_mount_is_left_unchanged(tmp_path: Path):
    raw = "/workspace/../etc/passwd"
    assert (
        remap_mcp_path_string(
            raw,
            workspace_root=tmp_path,
            workspace_mount="/workspace",
        )
        == raw
    )


def test_nested_resend_attachments(tmp_path: Path):
    payload = {
        "to": ["them@example.edu"],
        "attachments": [
            {
                "filename": "structure.cif",
                "filePath": "/workspace/projects/pm/structure.cif",
            }
        ],
        "html": "<p>see attached</p>",
    }
    out = remap_mcp_value(
        payload,
        workspace_root=tmp_path,
        workspace_mount="/workspace",
    )
    expected = str((tmp_path / "projects" / "pm" / "structure.cif").resolve())
    assert out["attachments"][0]["filePath"] == expected
    assert out["html"] == "<p>see attached</p>"
    assert out["to"] == ["them@example.edu"]


def test_wrap_rewrites_kwargs_before_forward(tmp_path: Path):
    seen = {}

    class _Tool:
        def forward(self, **kwargs):
            seen.update(kwargs)
            return "ok"

    tool = _Tool()
    wrap_mcp_tool_with_workspace_paths(
        tool,
        workspace_root=tmp_path,
        workspace_mount="/workspace",
    )
    result = tool.forward(
        filePath="/workspace/scratch/a.cif",
        attachments=[{"filePath": "/workspace/b.pdf", "filename": "b.pdf"}],
    )
    assert result == "ok"
    assert seen["filePath"] == str((tmp_path / "scratch" / "a.cif").resolve())
    assert seen["attachments"][0]["filePath"] == str((tmp_path / "b.pdf").resolve())
    assert seen["attachments"][0]["filename"] == "b.pdf"
