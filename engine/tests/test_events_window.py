"""An unknown window parameter on /events must fail loudly, not return the oldest rows.

Measured failure this pins: an agent asked for ``?offset=9999&order=desc``, got HTTP 200 with
seq 5 and 6 -- the oldest events in the org -- and reported two acceptance checks as NOT_RUN
because "the event log only exposes the oldest 5,000 and is 101 hours behind". The log was fine;
the parameter was ignored. A silent ignore turns "I read nothing" into "there is nothing", which
is the most expensive kind of false negative in an evidence-driven system.
"""

from __future__ import annotations

import pytest


async def _events(app_client, org_id: str, query: str = ""):
    return await app_client.get(f"/api/v1/orgs/{org_id}/events{query}")


async def test_unknown_parameter_is_refused_with_the_accepted_names(app_client):
    from tests.helpers import make_org

    org = await make_org(app_client)
    r = await _events(app_client, org["id"], "?offset=9999&order=desc")
    assert r.status_code == 400, (
        f"an ignored window parameter must not return 200 with the oldest rows: {r.status_code}")
    body = r.text
    for name in ("offset", "order"):
        assert name in body, f"the error must name the offending parameter: {body}"
    for name in ("after", "last", "limit"):
        assert name in body, f"the error must say what IS accepted: {body}"


async def test_known_parameters_still_work(app_client):
    from tests.helpers import make_org

    org = await make_org(app_client)
    for q in ("", "?after=0", "?limit=10", "?last=5"):
        r = await _events(app_client, org["id"], q)
        assert r.status_code == 200, f"{q or '(no query)'} must keep working: {r.status_code}"
