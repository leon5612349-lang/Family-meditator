"""Тесты адресации шага: get_step_recipient и поля addressed_to/current_step
в /api/history (задача «каждый видит только свой вопрос»).
"""

import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database  # noqa: E402
import therapy  # noqa: E402

CREATOR = 631630724   # Партнёр 1 (id сессии = id создателя)
PARTNER = 999000111   # Партнёр 2


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(
        database, "DB_FILE", os.path.join(str(tmp_path), "mediator.db")
    )
    asyncio.run(database.init_db())
    return database


def test_get_step_recipient_mapping():
    """_A → создатель, _B → партнёр, _LAKE/INTEGRATION → None (шаг общий)."""
    assert therapy.get_step_recipient(
        "CASCADE_1_A", CREATOR, CREATOR, PARTNER
    ) == CREATOR
    assert therapy.get_step_recipient(
        "CASCADE_2_B", CREATOR, CREATOR, PARTNER
    ) == PARTNER
    assert therapy.get_step_recipient(
        "CASCADE_1_LAKE", CREATOR, CREATOR, PARTNER
    ) is None
    assert therapy.get_step_recipient(
        "CASCADE_3_INTEGRATION", CREATOR, CREATOR, PARTNER
    ) is None
    assert therapy.get_step_recipient("", CREATOR, CREATOR, PARTNER) is None


@pytest.fixture
def http(fresh_db):
    """httpx-клиент поверх api.app (lifespan не поднимаем)."""
    import api

    class _Wrap:
        def __call__(self):
            return self

        async def __aenter__(self):
            self._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api.app),
                base_url="http://test",
            )
            return self._client

        async def __aexit__(self, *exc):
            await self._client.aclose()

    return _Wrap()


async def _seed(step):
    await database.delete_session(CREATOR)
    await database.delete_session(PARTNER)
    await database.upsert_user(CREATOR, "Anton", CREATOR)
    await database.update_profile(CREATOR, "Anton", "М", "опоздания")
    await database.upsert_user(PARTNER, "PartnerTest", CREATOR)
    await database.update_profile(PARTNER, "PartnerTest", "Ж", "тест")
    await database.set_current_step(CREATOR, step)


@pytest.mark.asyncio
async def test_history_exposes_addressed_to(fresh_db, http):
    """На ступени A адресат — создатель; партнёр получает addressed_to и
    НЕ получает текст вопроса."""
    await _seed("CASCADE_1_A")

    async with http() as ac:
        r_creator = (await ac.get(
            f"/api/history?user_id={CREATOR}&after_id=0")).json()
        r_partner = (await ac.get(
            f"/api/history?user_id={PARTNER}&after_id=0")).json()

    assert r_creator["current_step"] == "CASCADE_1_A"
    assert r_creator["addressed_to"] == CREATOR
    assert r_creator["masked"] is False
    assert r_creator["step_text"]

    assert r_partner["current_step"] == "CASCADE_1_A"
    assert r_partner["addressed_to"] == CREATOR
    assert r_partner["masked"] is True
    assert r_partner["step_text"] is None
    assert "Что вы чувствуете" not in str(r_partner)


@pytest.mark.asyncio
async def test_history_step_b_addressed_to_partner(fresh_db, http):
    """На ступени B адресат — партнёр."""
    await _seed("CASCADE_1_B")

    async with http() as ac:
        r_creator = (await ac.get(
            f"/api/history?user_id={CREATOR}&after_id=0")).json()
        r_partner = (await ac.get(
            f"/api/history?user_id={PARTNER}&after_id=0")).json()

    assert r_creator["addressed_to"] == PARTNER
    assert r_creator["masked"] is True
    assert r_partner["addressed_to"] == PARTNER
    assert r_partner["masked"] is False
    assert r_partner["step_text"]


@pytest.mark.asyncio
async def test_history_lake_addressed_to_none(fresh_db, http):
    """В Озере шаг общий: addressed_to = None, текст видят оба."""
    await _seed("CASCADE_1_LAKE")

    async with http() as ac:
        r_creator = (await ac.get(
            f"/api/history?user_id={CREATOR}&after_id=0")).json()
        r_partner = (await ac.get(
            f"/api/history?user_id={PARTNER}&after_id=0")).json()

    assert r_creator["addressed_to"] is None
    assert r_partner["addressed_to"] is None
    assert r_creator["masked"] is False
    assert r_partner["masked"] is False
    assert r_partner["step_text"]
