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

from app.models import Artifact, Org, Turn
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
    # Count FINISHED turns before, and compare like with like. Counting all turns and then comparing
    # against only the finished ones made the condition unreachable whenever an earlier turn was
    # still running: `before` included it, so len(completed) could never exceed it. That is a 30s
    # timeout, which reads as a slow test rather than a broken predicate.
    def finished(ts):
        return [t for t in ts if t.status in ("completed", "failed")]

    before = len(finished(await rows(Turn, Turn.agent_id == agent["id"])))
    script(agent["id"], calls(("write_file", {"path": path, "content": body})), say("wrote it"))
    await dm(client, org_id, name, "please write it")

    async def done() -> bool:
        return len(finished(await rows(Turn, Turn.agent_id == agent["id"]))) > before

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


async def test_the_same_bytes_in_two_runs_are_two_receipts_not_one(app_client):
    """This test used to assert the opposite, and that assertion WAS the bug.

    It required one row when two different Runs wrote identical bytes, which forced `record()` to
    overwrite the first row's turn_id -- so the earlier Run's receipt vanished and its P0-3 became a
    hole for no reason. "Don't fill the list with copies" is right for one Run retrying; it is wrong
    across Runs, where each Run really did produce those bytes and must be able to say so.

    The dedupe that still matters -- one Run writing the same bytes twice -- is asserted separately
    (`test_the_same_run_writing_the_same_bytes_twice_is_one_row`).
    """
    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    first = await _run_writing(app_client, org["id"], agent, "same\n")
    second = await _run_writing(app_client, org["id"], agent, "same\n")

    rows_ = await rows(Artifact, Artifact.path == "out.md")
    assert len(rows_) == 2, f"two Runs, two receipts: {[(r.turn_id, r.sha256) for r in rows_]}"
    assert {r.turn_id for r in rows_} == {first, second}


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


async def test_the_source_commit_is_captured_inside_a_git_checkout(app_client, tmp_path):
    """sourceSha must be the commit the file sat on -- and null (not "clean") outside a repo.

    Kit's T-67 receipt flagged sourceSha=null as an engine gap. It was not: the file was written at
    the workspace root, which is not a checkout, and NULL is what the code promises there. This
    test pins both halves so the next reader can tell a real gap from the documented NULL.
    """
    import subprocess

    from app.models import Agent as AgentModel, Org
    from app.tools.base import org_workspace

    def git(*args, cwd):
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)

    org = await make_org(app_client)
    agent = await make_agent(app_client, org["id"], "Ann")
    repo = org_workspace((await rows(Org, Org.id == org["id"]))[0]) / "checkout"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "T", cwd=repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "seed", cwd=repo)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True,
                          text=True, check=True).stdout.strip()

    before = len([t for t in await rows(Turn, Turn.agent_id == agent["id"])
                  if t.status in ("completed", "failed")])
    script(agent["id"], calls(("write_file", {"path": "checkout/notes.md", "content": "n\n"})),
           say("ok"))
    await dm(app_client, org["id"], "Ann", "write in the checkout")

    async def done() -> bool:
        return len([t for t in await rows(Turn, Turn.agent_id == agent["id"])
                    if t.status in ("completed", "failed")]) > before

    await wait_for(done, msg="turn", timeout=30)

    inside = (await rows(Artifact, Artifact.path == "checkout/notes.md"))[0]
    assert inside.source_sha == head, (
        f"the file sits on commit {head[:7]}; the receipt must say so: {inside.source_sha}")
    assert inside.source_dirty is True, "a new untracked file differs from the commit"

    # The same turn's file outside any checkout: null, and that means "no repo", not "clean".
    script(agent["id"], calls(("write_file", {"path": "loose.md", "content": "l\n"})), say("ok"))
    await dm(app_client, org["id"], "Ann", "write at the root")

    async def done2() -> bool:
        return len([t for t in await rows(Turn, Turn.agent_id == agent["id"])
                    if t.status in ("completed", "failed")]) > before + 1

    await wait_for(done2, msg="second turn", timeout=30)
    outside = (await rows(Artifact, Artifact.path == "loose.md"))[0]
    assert outside.source_sha is None and outside.source_dirty is None


async def test_two_runs_writing_identical_bytes_each_keep_their_receipt(app_client):
    """Measured bug: the second byte-identical write moved the first Run's turn_id away.

    The unique key was (org, path, sha256), so `record()` found the first Run's row and updated its
    turn_id to the second Run. Consequence: `/turns/{first}` returned an empty artifact list and the
    first Run's P0-3 silently became "recorded nothing" -- a hole that appeared out of a no-op edit.
    Both Runs wrote those bytes, so both must be able to prove it.
    """
    org = await make_org(app_client)
    a = await make_agent(app_client, org["id"], "First")
    b = await make_agent(app_client, org["id"], "Second")
    body = "identical\n"

    turn_a = await _run_writing(app_client, org["id"], a, body, path="same.md", name="First")
    turn_b = await _run_writing(app_client, org["id"], b, body, path="same.md", name="Second")

    rows_ = await rows(Artifact, Artifact.path == "same.md")
    assert len(rows_) == 2, f"one receipt per Run, got {len(rows_)}: {[(r.turn_id, r.sha256) for r in rows_]}"
    by_turn = {r.turn_id: r for r in rows_}
    assert set(by_turn) == {turn_a, turn_b}, "each Run keeps its own row"
    assert by_turn[turn_a].sha256 == by_turn[turn_b].sha256, "same bytes, by construction"

    # The earlier Run must still report the artifact -- this is the assertion the bug failed.
    r = await app_client.get(f"/api/v1/turns/{turn_a}")
    assert r.status_code == 200
    mine = r.json()["artifacts"]
    assert len(mine) == 1, f"the earlier Run's receipt must survive a later identical write: {mine}"
    assert mine[0]["path"] == "same.md"
    assert mine[0]["turnId"] == turn_a, "and it must still name the Run that wrote it first"

    # And the later Run has its own.
    r = await app_client.get(f"/api/v1/turns/{turn_b}")
    assert [x["turnId"] for x in r.json()["artifacts"]] == [turn_b]


async def test_the_same_run_writing_the_same_bytes_twice_is_one_row(app_client):
    """What the key was actually for: a Run's own retry must not fill the list with copies."""
    from app.models import Artifact
    org = await make_org(app_client)
    a = await make_agent(app_client, org["id"], "Retrier")

    before = len([t for t in await rows(Turn, Turn.agent_id == a["id"])
                  if t.status in ("completed", "failed")])
    script(a["id"],
           calls(("write_file", {"path": "twice.md", "content": "same\n"})),
           calls(("write_file", {"path": "twice.md", "content": "same\n"})),
           say("wrote it twice"))
    await dm(app_client, org["id"], "Retrier", "write it twice")

    async def done() -> bool:
        return len([t for t in await rows(Turn, Turn.agent_id == a["id"])
                    if t.status in ("completed", "failed")]) > before

    await wait_for(done, msg="turn", timeout=30)
    rows_ = await rows(Artifact, Artifact.path == "twice.md")
    assert len(rows_) == 1, f"one Run, one version, one row -- got {len(rows_)}"
