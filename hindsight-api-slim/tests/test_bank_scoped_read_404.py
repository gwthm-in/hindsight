"""Regression (#4175): bank-scoped read endpoints must 404 for a bank that does not exist.

They used to answer 200 with an empty payload — zeroed counters from ``/stats``,
an empty page from ``/memories/list`` — which is byte-identical to a healthy,
freshly-created bank. Any monitor built on those endpoints therefore kept passing
after the bank it watched was renamed, deleted or recreated under another id, and
a typo in ``bank_id`` was never surfaced at all.

The check runs after each endpoint's own read authorization, so it widens nothing:
a caller that may not read the bank still gets its usual authorization error, not
a 404 that would leak the bank's existence.
"""

import uuid

import httpx
import pytest
import pytest_asyncio

from hindsight_api import RequestContext
from hindsight_api.api import create_app


@pytest_asyncio.fixture
async def api_client(memory):
    app = create_app(memory, initialize_memory=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# Every bank-scoped GET that aggregates or lists, i.e. the ones whose empty
# answer is indistinguishable from "bank exists and is empty". Sub-resource GETs
# (``/memories/{id}``, ``/mental-models/{id}``, ...) already 404 on the missing
# child and are not listed here.
READ_ENDPOINTS = [
    ("graph", {}),
    ("memories/list", {"limit": 1}),
    ("stats", {}),
    ("stats/memories-timeseries", {}),
    ("entities", {}),
    ("entities/graph", {}),
    ("mental-models", {}),
    ("knowledge-base/tree", {}),
    ("knowledge-base/export", {}),
    ("knowledge-base/search", {"q": "anything"}),
    ("directives", {}),
    ("documents", {}),
    ("tags", {}),
    ("operations", {}),
    ("observations/scopes", {}),
    ("config", {}),
    ("webhooks", {}),
]


def _url(bank_id: str, suffix: str) -> str:
    return f"/v1/default/banks/{bank_id}/{suffix}"


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,params", READ_ENDPOINTS, ids=[s for s, _ in READ_ENDPOINTS])
async def test_missing_bank_returns_404(api_client, suffix, params):
    bank_id = f"nosuch-{uuid.uuid4().hex[:8]}"
    resp = await api_client.get(_url(bank_id, suffix), params=params)
    assert resp.status_code == 404, resp.text
    assert bank_id in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,params", READ_ENDPOINTS, ids=[s for s, _ in READ_ENDPOINTS])
async def test_existing_empty_bank_still_returns_200(api_client, memory, suffix, params):
    # Positive control: an empty bank that DOES exist must keep answering 200,
    # so the 404 above distinguishes "missing" from "empty" rather than
    # replacing one indistinguishable answer with another.
    bank_id = f"empty-{uuid.uuid4().hex[:8]}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=RequestContext())
    resp = await api_client.get(_url(bank_id, suffix), params=params)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_read_does_not_create_the_bank(api_client, memory):
    # The 404 must come from a read-only existence check: a client polling a
    # stale bank_id must not silently materialise that bank.
    bank_id = f"nosuch-{uuid.uuid4().hex[:8]}"
    assert (await api_client.get(_url(bank_id, "stats"))).status_code == 404
    profile = await memory.get_bank_profile(bank_id, request_context=RequestContext(), create_if_missing=False)
    assert profile is None
