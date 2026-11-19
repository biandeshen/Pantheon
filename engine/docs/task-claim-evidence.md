# Task claiming: the five layers, and what each one actually proves

This file exists because "the tool is implemented" and "an agent can claim work" are three
layers apart, and every layer in between can fail while the one before it looks fine. It
records what was measured at each layer, the ids needed to re-check it, and — at the end —
the layer that is still **not** satisfied.

Nothing in here is a screenshot of a secret. Credentials appear only as booleans.

## Why the layer distinction matters

`claim_task` shipped, had a schema, and answered correctly over HTTP. The 13 endpoint tests
were green. An agent still could not claim anything, because:

- the tool was not in the employee's stored tool list, and
- `resolve_tools` marks `TEAM_CORE` members `core` only `if name in tools` — it never adds
  a tool the config omits, so a missing entry stays missing no matter what the constant says.

So the endpoint was correct and the capability was absent. The layers below are ordered by
how far they are from the model actually acting.

## Layer 1 — builtin: the tool is defined

`app/tools/collab.py` registers `claim_task`. Endpoint path `POST /api/v1/tasks/{id}/claim`
and `POST /api/v1/orgs/{org}/tasks/claim-next`.

Proves: the function exists. Nothing about reachability.

## Layer 2 — configured: the employee's stored tool list contains it

Measured on 2026-11-19 after the config correction, read back through `GET /api/v1/agents/{id}`:

| employee | tools in config | `claim_task` entry |
|---|---|---|
| Juno (Design lead) | 61 | `{"name": "claim_task", "approval": "auto"}` |
| Kit (Builder) | 63 | `{"name": "claim_task", "approval": "auto"}` |
| Tess (Tester) | 61 | `{"name": "claim_task", "approval": "auto"}` |

Before the correction the same read showed the entry absent for all three, while
`list_tasks`, `update_task`, `create_task`, `get_task` and `send_message` were present —
i.e. only this one member of the coordination set was missing.

The correction went through `PATCH /api/v1/agents/{id}`, which validates tool names, rather
than by mutating the row. An explicit `{"name": "claim_task", "approval": "deny"}` still wins;
nothing bulk-granted `TEAM_CORE` to employees who had not been given it.

The change is visible in the event log as three `agent.updated` records:

| seq | timestamp (UTC) | employee | tools | `claim_task` |
|---|---|---|---|---|
| 14464 | 2026-11-19T19:58:56 | Juno | 61 | true |
| 14465 | 2026-11-19T19:59:33 | Kit | 63 | true |
| 14466 | 2026-11-19T19:59:33 | Tess | 61 | true |

Proves: the config now lists it. Still nothing about what reaches the model.

## Layer 3 — resolved: `resolve_tools` keeps it and marks it core

`app/tools/resolve.py` builds the set from the agent's config and then marks `TEAM_CORE`
members core. With the entry present, it survives resolution and is flagged `core`, which is
what lets a lean loadout offer it every turn.

Regression guard: `tests/test_claim.py::test_team_core_is_read_by_split` and
`::test_claim_task_is_offered_without_loading_it_first`. Both were confirmed red against the
unfixed `split()` before landing (`2 failed, 13 deselected`), then green.

Proves: resolution keeps it. Not that the request carries it.

## Layer 4 — offered: the real request body contains it

This is the layer that had been failing silently. Read directly out of `llm_calls.request`,
which stores the exact payload sent to the model:

| employee | llm_call id | turn id | sent at (UTC) | tools offered | `claim_task` in `"tools": [...]` |
|---|---|---|---|---|---|
| Juno | `llm_01m7tsxftp4d9ywm1780` | `trn_01m7t3j1ejwz8cb6jmx4` | 2026-11-19T21:03:04 | 20 | yes |
| Kit | `llm_01m7tsxpnfyrh2wwb6g9` | `trn_01m7tnk5cg6p5md5gzcc` | 2026-11-19T21:03:11 | 18 | yes |
| Tess | `llm_01m7tsx77cf652nar5v3` | `trn_01m7tch3gkn14g6ky8nx` | 2026-11-19T21:02:55 | 15 | yes |

The offered set is a flat string array (`"tools": ["cancel_schedule", "claim_task", ...]`),
not objects with `name` fields. Parsing for `"name": "<tool>"` there returns nothing and
looks like absence; an earlier reading of mine reported exactly that false negative. Check
the shape before believing the value.

Proves: the model is told the tool exists. Nothing about it being used.

## Layer 5 — invoked: a fresh agent actually calls it

**Not satisfied.** Measured on the same database:

| counter | value |
|---|---|
| `tool_calls` where `name = 'claim_task'` | **0** |
| `tool_calls` where `name = 'load_tools'` | 16 |
| `tool_calls` where `name = 'update_task'` | 185 |

Two recorded attempts show the gap from the other side. At 19:22:39 Kit asked for the tool
by name and was told it did not exist — this is the pre-fix state, and it is the clearest
statement of the bug:

```
#14063 19:22:39  tool.started    2rs5rngx  load_tools {"names": ["claim_task"]}
#14067 19:22:39  tool.completed  2rs5rngx  load_tools ERROR: you have no tool named claim_task
#14073 19:22:45  agent.thought   2rs5rngx  "我没有 claim_task 工具（load_tools 返回 ...）"
```

And at 20:02:45 the work that actually moved T-49 was an HTTP PATCH inside a script, not a
tool call:

```
#14471 20:02:45  approval.decided  5y75eyvm  run_command  {"command": "node r2-deliver.mjs"}
```

`r2-deliver.mjs` contains `method: 'PATCH'` and a body setting `status`, `assigneeId` and
`reviewerId` directly. That is the operator path. It is a legitimate way for a person to
reassign work; it is not evidence that an agent claimed work for itself.

Proves: nothing yet. This is the open item.

## What T-49 actually was, and who did what

The task changed hands twice, and the summary has to follow the event log rather than any
one report:

| # | time (UTC) | actor | event |
|---|---|---|---|
| 267 | 15:54:16 | user | `created`, assignee null |
| 268 | 15:54:35 | Juno | `claimed` → in_progress, `forced: false` |
| 279 | 19:22:46 | Kit | `assignee`: Juno → Kit, via `update_task` after `load_tools` refused |
| 280 | 19:28:31 | Kit | `status`: in_progress → review |
| 281 | 19:47:31 | system | `acceptance` round 1, **passed: false** |
| 282 | 19:47:31 | system | `status`: review → in_progress (back to the assignee) |
| 284 | 20:02:46 | user | `assignee`: Kit → Juno, via `r2-deliver.mjs` PATCH |
| 285 | 20:02:46 | user | `status`: in_progress → review |

So: **Juno authored the first delivery, Kit authored the second, and the final hand-back to
Juno was done by a script using the user identity — not by an agent claiming.** The current
assignee is Juno because of that PATCH, not because Juno claimed it back.

Two deliverables exist, by two different authors:

| file | author | bytes | sha256 |
|---|---|---|---|
| `r2-t49-d06-source-traceability.md` | Juno | 2907 | `cd29042d9b5d3841db752cf2e0356b383e8c612e9f8e83a7536278706a9e8f70` |
| `t49-knowledge-source-audit.md` | Kit | 11906 | `dc8b2c28e2d91c25b726f2474f268f046477ced3f33e460faf3c7bdec15107cc` |

`Task.result` (1487 bytes, sha256 `30d04f74181d57dc79e355f92858d337dd511940c1da396993573415c176dc30`)
points at the Kit file and records that v1 and v2 of it were wrong: v1 claimed "13 rows, no
omissions" while at least 20 tasks were neither listed nor excluded; v2 reported 55 rows
after patching but actually had 34 lines with 19 duplicates. v3 rewrote it and self-checked
55/55 unique with none missing.

## Acceptance: what the engine's own check found

Round 1 of 2 failed on four points, kept here because the failures are more informative than
a pass would have been:

1. **Coverage was wrong.** Five refs were excluded as "single-line probes" on a false premise,
   and at least 11 existing tasks were never mentioned at all, against a claim of "no
   omissions". T-10 is the same kind of deliverable as admitted tasks and was excluded anyway.
2. **"Actual source" was not actual for several rows** — generic CLI templates with literal
   `...` placeholders, a relative path with no directory, and T-36's bare filenames that do not
   resolve without the `audit-repo/evidence/` prefix.
3. **T-47's provenance was unverified rather than proven** — the filenames cited appear in
   T-46's result, not T-47's.
4. **The result line overstated the work** — "全部列出，无遗漏" while a third of the tasks had
   not been read.

It also confirmed what was genuinely correct: real workspace artefacts were cited for the
has-source rows, no row used 「我认为/应该」 as evidence, status was `review` not `done`, and
the 14-domain report was not passed off as this deliverable.

**No verdict from the named reviewer exists yet.** The acceptance above was produced by the
engine's own fresh-context checker, which is not the same thing as the reviewer of record.
`reviewerId` is set to Tess; Tess has produced no message or event asserting a verdict on
T-49. So the task sits in `review` with an independent check having failed once and no
sign-off — that is the honest state.

## Not run

- **Resolver regression through the real agent row: NOT RUN.** The test would call
  `resolve_tools` from inside the `app_client` fixture, and `resolve_tools` opens its own
  session, which `db_strict` refuses as a nested transaction. The fix is to let the fixture's
  transaction end before calling the resolver, not to relax `db_strict`. Until then, layer 3
  rests on the two `split()` tests plus layer 4's real request, which is stronger evidence
  anyway but is not the same test.
- **Failure recovery / cold-start takeover: NOT RUN.** Claim persistence across a restart was
  verified earlier; what has not been shown is a second agent picking up a task whose holder
  died mid-work, with the partial result preserved.
- **Concurrency on Postgres: NOT MEASURED.** The 409 guarantee rests on SQLite `BEGIN
  IMMEDIATE` plus `with_for_update()`. Under Postgres the row lock serialises correctly but the
  ORM instances read before the lock may be stale. Registered as P2 for the engine; it does not
  affect the current SQLite delivery.

## The one-line state

Layers 1 through 4 hold. Layer 5 — a fresh agent calling `claim_task` for itself, with tools
offering it, `ctx.sender` as the claimant, a native `claimed` event and a surviving turn —
remains **NOT VERIFIED**. Until that happens, claim is READY, not working, and R2 is not
satisfied.
