"""The claim_task tool must be callable and must read identity from the context.

The endpoint tests prove the service layer. These prove the layer an agent actually reaches:
if the tool raises, or takes the claimant from an argument, an agent cannot claim for itself
no matter how correct the service is underneath.
"""

from __future__ import annotations

from app.tools import collab
from app.tools.base import ToolContext, ToolError
from tests.helpers import make_agent, make_org, rows
from app.models import Agent, Org, Task
import pytest
from sqlalchemy import select


async def _ctx(org_id: str, agent_id: str) -> ToolContext:
    from app.core.db import SessionLocal

    async with SessionLocal() as s:
        org = (await s.execute(select(Org).where(Org.id == org_id))).scalar_one()
        agent = (await s.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
    return ToolContext(org=org, agent=agent, turn_id=None, tool_call_id="t", depth=0)


async def test_tool_claims_for_the_calling_agent(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    r = await app_client.post(f"/api/v1/orgs/{org['id']}/tasks",
                              json={"title": "T", "description": "d", "status": "todo"})
    task = r.json()

    ctx = await _ctx(org["id"], agent["id"])
    out = await collab.claim_task({"task": task["ref"]}, ctx)
    assert "Claimed" in out, out

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id == agent["id"], "the tool must assign the calling agent"


async def test_tool_takes_identity_from_context_not_from_arguments(app_client):
    """Passing someone else's id as an argument must not change who gets the task."""
    org = await make_org(app_client)
    ann = await make_agent(app_client, org["id"], "Ann")
    bob = await make_agent(app_client, org["id"], "Bob")
    r = await app_client.post(f"/api/v1/orgs/{org['id']}/tasks",
                              json={"title": "T2", "description": "d", "status": "todo"})
    task = r.json()

    ctx = await _ctx(org["id"], ann["id"])
    # `agent`/`assignee`/`agentId` are not part of the schema at all; if one were honoured
    # the task would land on Bob while Ann made the call.
    out = await collab.claim_task({"task": task["ref"], "agent": bob["id"],
                                   "agentId": bob["id"], "assignee": bob["id"]}, ctx)
    assert "Claimed" in out, out

    stored = (await rows(Task, Task.id == task["id"]))[0]
    assert stored.assignee_id == ann["id"], (
        "identity must come from ctx.sender; an argument naming another agent must be ignored")


async def test_tool_says_so_when_nothing_is_free(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    ctx = await _ctx(org["id"], agent["id"])

    out = await collab.claim_task({}, ctx)
    assert "Nothing free" in out, f"an empty queue must be reported, not invented: {out}"
