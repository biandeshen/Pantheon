"""file_digest: the engine computes the hash, so a review can quote it as evidence.

Written after a real dispute: T-49's verdict named the artifact and its byte count but not its
sha256, and the acceptance clause requires the hash. The proposed workaround was for the
reviewer to shell out to node -- which needs an approval and yields a number attributable to the
model rather than to the engine. A digest tool removes both problems.
"""

from __future__ import annotations

import hashlib
import pytest

from app.tools import filetools
from app.tools.base import ToolContext, ToolError
from tests.helpers import make_agent, make_org


async def _ctx(org_id: str, agent_id: str) -> ToolContext:
    from app.core.db import SessionLocal
    from app.models import Agent, Org
    from sqlalchemy import select

    async with SessionLocal() as s:
        org = (await s.execute(select(Org).where(Org.id == org_id))).scalar_one()
        agent = (await s.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
    return ToolContext(org=org, agent=agent, turn_id=None, tool_call_id="t", depth=0)


async def test_digest_is_the_real_sha256_and_the_real_size(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    ctx = await _ctx(org["id"], agent["id"])
    payload = b"T-49 artifact under review\n"
    (ctx.workdir / "audit.md").write_bytes(payload)
    out = await filetools.file_digest({"paths": ["audit.md"]}, ctx)

    expected = hashlib.sha256(payload).hexdigest()
    assert expected in out, f"the digest must be the file's real sha256: {out!r}"
    assert str(len(payload)) in out, f"the byte count must be reported: {out!r}"
    assert "audit.md" in out
    # One line per file, so it can be quoted verbatim.
    assert len(out.strip().splitlines()) == 1


async def test_digest_changes_when_the_content_changes(app_client):
    """The point of binding a verdict to a hash: a changed file must not match."""
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    ctx = await _ctx(org["id"], agent["id"])
    artifact = ctx.workdir / "audit.md"

    artifact.write_bytes(b"version A\n")
    before = await filetools.file_digest({"paths": ["audit.md"]}, ctx)
    artifact.write_bytes(b"version B\n")
    after = await filetools.file_digest({"paths": ["audit.md"]}, ctx)

    assert before.split()[0] != after.split()[0], (
        f"a rewritten file must digest differently: {before!r} vs {after!r}")


async def test_digest_refuses_a_missing_file_and_a_folder(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    ctx = await _ctx(org["id"], agent["id"])
    (ctx.workdir / "sub").mkdir()

    with pytest.raises(ToolError):
        await filetools.file_digest({"paths": ["nope.md"]}, ctx)
    with pytest.raises(ToolError):
        await filetools.file_digest({"paths": ["sub"]}, ctx)


async def test_digest_refuses_a_path_outside_the_workspace(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    ctx = await _ctx(org["id"], agent["id"])

    with pytest.raises(ToolError):
        await filetools.file_digest({"paths": ["../../secrets.txt"]}, ctx)


async def test_digest_without_paths_is_an_error_not_a_walk(app_client):
    """A folder digest has no single meaning; silently walking would invite quoting the wrong one."""
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    ctx = await _ctx(org["id"], agent["id"])
    with pytest.raises(ToolError):
        await filetools.file_digest({}, ctx)
