"""An artifact row is a receipt: which Run wrote which version, and the hash of the bytes.

Written after P0-3 could not be answered from the engine's own records. The table had a task_id
column and no writer ever set it (326 rows, 0 with a task), so the only way to say what a Run
produced was `git rev-list --before=<endedAt>` -- which credited a later commit to an earlier run.
The chairman's ruling is the acceptance clause here: "验收读当次 Run 明确记录并可回查的产物版本，
当前 HEAD 另列；归属无证据就标缺，不靠时间推定."

So these tests assert the record, not a reconstruction:

  * a file written during a task's turn is bound to that task AND that Run;
  * the sha256 is the file's real digest, computed by the engine;
  * a Run that wrote nothing reports nothing (no fallback to "newest file");
  * a second write of different bytes is a second version, so the first stays answerable;
  * a rewrite of the same bytes stays one row instead of filling the list with copies.
"""

from __future__ import annotations

import hashlib

from app.models import Artifact, Turn
from app.services import tasks
from app.services.comms import Sender
from tests.conftest import wait_for
from tests.helpers import calls, dm, make_agent, make_org, rows, say, script


async def served_digest(client, org_id: str, rel: str) -> str:
    """The hash a reviewer gets by re-reading the file through the app's own file route.

    Deliberately not from the artifact row: the point of the receipt is that the number can be
    recomputed from the bytes a reader actually opens, so the test does exactly that.
    """
    r = await client.get(f"/api/v1/orgs/{org_id}/files/raw", params={"path": rel})
    assert r.status_code == 200, r.text
    return hashlib.sha256(r.content).hexdigest()


async def _run_writing(client, org_id: str, agent: dict, body: str, *, path="out.md",
                       name: str = "Ann") -> str:
    """One turn of `agent` that writes one file; returns the turn id of *this* turn.

    Counts turns before and waits for a new finished one: "some turn finished" is already true
    after the first call, and waiting on that would hand back the previous turn's id -- which is
    how a receipt test silently checks the wrong Run.
    """
    before = len(await rows(Turn, Turn.agent_id == agent["id"]))
    script(agent["id"], calls(("write_file", {"path": path, "content": body})), say("wrote it"))
    await dm(client, org_id, name, "please write it")

    async def done() -> bool:
        done_ = [t for t in await rows(Turn, Turn.agent_id == agent["id"])
                 if t.status in ("completed", "failed")]
        return len(done_) > before

    await wait_for(done, msg="turn", timeout=30)
    return (await rows(Turn, Turn.agent_id == agent["id"]))[-1].id


async def test_a_write_during_a_task_turn_is_bound_to_that_task_and_run(app_client):
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    task = await tasks.create_task(org["id"], Sender.user(), title="Write the brief",
                                   assignee="Ann")

    async def done() -> bool:
        return any(t.status in ("completed", "failed") for t in
                   await rows(Turn, Turn.agent_id == agent["id"]))

    script(agent["id"], calls(("write_file", {"path": "brief.md", "content": "# Brief\n"})),
           say("done"))
    await wait_for(done, msg="turn", timeout=30)
    turn = (await rows(Turn, Turn.agent_id == agent["id"]))[-1]

    art = (await rows(Artifact, Artifact.path == "brief.md"))[0]
    assert art.task_id == task.id, (
        "the artifact must name the task the Run was working on, not just the org and path")
    assert art.turn_id == turn.id, "the artifact must name the Run that wrote it"
    on_disk = await served_digest(app_client, org["id"], "brief.md")
    assert art.sha256 == on_disk, (
        "the digest must be the hash of the bytes actually on disk, so anyone can recompute it")
    assert len(art.sha256) == 64
    assert art.size > 0


async def test_the_turn_endpoint_reports_what_that_run_produced(app_client):
    """The receipt has to be readable from the Run, or a reviewer still has to guess."""
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    await tasks.create_task(org["id"], Sender.user(), title="Write it", assignee="Ann")

    async def done() -> bool:
        return any(t.status in ("completed", "failed") for t in
                   await rows(Turn, Turn.agent_id == agent["id"]))

    script(agent["id"], calls(("write_file", {"path": "a.md", "content": "A\n"})), say("ok"))
    await wait_for(done, msg="turn", timeout=30)
    turn = (await rows(Turn, Turn.agent_id == agent["id"]))[-1]

    r = await app_client.get(f"/api/v1/turns/{turn.id}")
    assert r.status_code == 200
    made = r.json()["artifacts"]
    assert [a["path"] for a in made] == ["a.md"], (
        f"the Run must list the version it recorded: {made!r}")
    assert made[0]["sha256"] == await served_digest(app_client, org["id"], "a.md")
    assert made[0]["taskId"], "and it must say which task it belongs to"


async def test_a_run_that_wrote_nothing_reports_nothing(app_client):
    """No fallback to the newest file in the org: that is the inference this table replaced."""
    org = await make_org(app_client)
    writer = await make_agent(app_client, org["id"], "Ann")
    reader = await make_agent(app_client, org["id"], "Bob")
    await _run_writing(app_client, org["id"], writer, "written by Ann\n")

    script(reader["id"], say("I only read."))
    await dm(app_client, org["id"], "Bob", "just answer, write nothing")

    async def done() -> bool:
        return any(t.status in ("completed", "failed") for t in
                   await rows(Turn, Turn.agent_id == reader["id"]))

    await wait_for(done, msg="turn", timeout=30)
    turn = (await rows(Turn, Turn.agent_id == reader["id"]))[-1]

    r = await app_client.get(f"/api/v1/turns/{turn.id}")
    assert r.json()["artifacts"] == [], (
        "a Run that wrote nothing must report an empty list, not someone else's file")


async def test_two_versions_of_one_path_stay_two_receipts(app_client):
    """The point of a receipt: after a rewrite, the version the first Run produced is still there."""
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    first = await _run_writing(app_client, org["id"], agent, "version one\n")
    second = await _run_writing(app_client, org["id"], agent, "version two\n")

    rows_ = await rows(Artifact, Artifact.path == "out.md")
    assert len(rows_) == 2, f"a rewritten file is a new version: {[(a.sha256, a.turn_id) for a in rows_]}"
    by_turn = {a.turn_id: a for a in rows_}
    assert by_turn[first].sha256 != by_turn[second].sha256, (
        "two versions must not share a digest, or the first one is unrecoverable")


async def test_writing_the_same_bytes_twice_is_one_row(app_client):
    """Otherwise the file list fills with copies of one file and the receipt stops meaning anything."""
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    await _run_writing(app_client, org["id"], agent, "same\n")
    await _run_writing(app_client, org["id"], agent, "same\n")

    assert len(await rows(Artifact, Artifact.path == "out.md")) == 1


async def test_a_chat_turn_with_no_task_leaves_task_id_empty(app_client):
    """A file written outside any task is a finding, not something to pin on the agent's last task."""
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    await tasks.create_task(org["id"], Sender.user(), title="Old task", assignee="Ann")
    # Let the task turn finish, then write again in a plain chat turn.
    await _run_writing(app_client, org["id"], agent, "from the task\n", path="task.md")
    await _run_writing(app_client, org["id"], agent, "from chat\n", path="chat.md")

    chat = (await rows(Artifact, Artifact.path == "chat.md"))[0]
    assert chat.task_id is None, (
        f"a chat turn names no task; borrowing one would be a guess: task_id={chat.task_id!r}")
    assert chat.turn_id, "but the Run is still recorded"
