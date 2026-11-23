"""run_command must describe its real boundary, because agents read the description first.

Written after a concrete, expensive misunderstanding: I proposed a per-agent auto-approval for
run_command on the theory that `cwd` plus resolve_in_workspace confined it. The chairman checked
and both were wrong -- resolve_in_workspace only guards path arguments of file tools, and
workspace.py uses create_subprocess_shell, where cwd is a starting directory. core/guard.py says
of itself: "It is not a sandbox".

The description said "Run a shell command in your workdir", which reads as confinement. This test
pins the correction: if someone trims the paragraph back out, the next agent inherits the same
wrong belief and is one approval away from writing anywhere.
"""

from __future__ import annotations

from app.tools import base
from app.tools import workspace  # noqa: F401  -- importing registers run_command


def _description() -> str:
    spec = base.builtin("run_command")
    assert spec is not None, "run_command must be registered"
    return spec.description


def test_description_does_not_claim_the_workdir_is_a_boundary():
    text = _description()
    assert "STARTS" in text, "the description must say cwd is a starting point, not a fence"
    assert "not a sandbox" in text, "it must quote guard.py's own admission"


def test_description_points_at_the_unapproved_read_tools_instead():
    text = _description()
    for tool in ("read_file", "search_files", "list_dir", "file_digest"):
        assert tool in text, f"a reader deciding whether to shell out must be told about {tool}"


def test_the_approval_is_still_ask():
    """The honest description is not a reason to loosen the gate."""
    spec = base.builtin("run_command")
    assert spec.default_approval == "ask", (
        "run_command must stay approval='ask': the boundary it has is the user's, not the tool's")
    assert spec.side_effects is True
