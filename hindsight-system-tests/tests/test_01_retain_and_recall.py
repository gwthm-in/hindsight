"""The foundation story: something retained can be recalled.

Everything else in this suite builds on this working. A caller hands Hindsight a
sentence, the extraction model turns it into facts, the worker finishes the
background half of the retain, and a later question that never repeats the
original wording gets those facts back.

Note what this test does *not* touch: no engine object, no SQL, no internal
module. It talks to a server process over HTTP through the published client, so
it stays true across any refactor that keeps the API's promises — which is
exactly what makes it worth having.
"""

from __future__ import annotations

import pytest

from hindsight_system_tests.payloads import consolidation, extracted, fact

pytestmark = pytest.mark.asyncio


async def test_a_retained_memory_comes_back_from_recall(client, llm, bank_id, settled):
    llm.on_step("extract_facts", contains="Berlin").returns(
        extracted(
            fact(
                "Alice moved to Berlin in 2021",
                when="2021",
                where="Berlin",
                who="Alice",
                entities=["Alice", "Berlin"],
            ),
            fact("Alice plays the cello professionally", who="Alice", entities=["Alice", "cello"]),
        )
    )
    # Retain also triggers consolidation in the worker. This story is not about
    # observations, so the model is told to draw none — but it is told explicitly,
    # because a call nobody scripted is a gap in the test, not a detail.
    llm.on_step("consolidate").returns(consolidation())

    await client.aretain(
        bank_id=bank_id,
        content="Alice moved to Berlin in 2021 and works as a cellist.",
    )
    await settled(bank_id)

    response = await client.arecall(bank_id=bank_id, query="Where does Alice live?")

    recalled = [result.text for result in response.results]
    assert any("Berlin" in text for text in recalled), f"expected the Berlin fact back, got {recalled}"
