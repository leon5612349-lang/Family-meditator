"""Тест атомарности перехода из Озера (Аудит A1).

Сценарий: оба партнёра нажали «Дальше», и два обработчика (бот и Mini App)
пытаются перевести сессию ОДНОВРЕМЕННО. Раньше это давало три отдельные
SQL-транзакции (сброс флагов -> смена шага -> рассылка), и каскад мог
перепрыгнуть, а вопросы уходили дважды.

Теперь переход захватывается одним UPDATE (database.claim_lake_advance):
победитель ровно один, второй молча выходит.

Примечание про «in-memory SQLite»: database._connect() открывает НОВОЕ
соединение на каждый вызов, поэтому :memory: у каждой коннекции была бы
своя база. Тест использует временный файл (тот же приём, что в
test_privacy_and_idle.py).
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database  # noqa: E402 — путь вставлен строкой выше
import therapy  # noqa: E402

CREATOR = 631630724   # Партнёр 1 (id сессии)
PARTNER = 999000111   # Партнёр 2


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Временная база + схема (миграции init_db) + пара в Озере с обоими ready."""
    monkeypatch.setattr(
        database, "DB_FILE", os.path.join(str(tmp_path), "mediator.db")
    )
    asyncio.run(database.init_db())

    async def _seed():
        await database.upsert_user(CREATOR, "Anton", CREATOR)
        await database.update_profile(CREATOR, "Anton", "М", "опоздания")
        await database.upsert_user(PARTNER, "PartnerTest", CREATOR)
        await database.update_profile(PARTNER, "PartnerTest", "Ж", "тест")
        await database.set_current_step(CREATOR, "CASCADE_1_LAKE")
        # оба нажали «Дальше»
        await database.set_ready_for_next(CREATOR, True)
        await database.set_ready_for_next(PARTNER, True)

    asyncio.run(_seed())
    return database


@pytest.mark.asyncio
async def test_lake_advance_race(fresh_db, monkeypatch):
    """Два параллельных перехода из Озера: победитель ровно один."""
    # Считаем вызовы enter_step (рассылка вопросов новой ступени)
    entered = []

    async def fake_enter_step(session_id, step, bot=None):
        entered.append(step)

    monkeypatch.setattr(therapy, "enter_step", fake_enter_step)

    class NullBot:
        async def send_message(self, chat_id, text, **kw):
            pass

    results = await asyncio.gather(
        therapy.try_advance_from_lake(CREATOR, NullBot()),
        therapy.try_advance_from_lake(CREATOR, NullBot()),
    )

    # 1) переход случился ровно один раз (один enter_step)
    assert len(entered) == 1, entered
    # 2) ровно один вызов сообщил новую ступень
    advanced = [r for r in results if r is not None]
    assert len(advanced) == 1, results
    # 3) шаг ровно следующий, а не «через один»
    assert advanced[0] == "CASCADE_2_A", advanced
    assert await database.get_current_step(CREATOR) == "CASCADE_2_A"

    # 4) флаги готовности сброшены
    async def _flags():
        ready, total = await database.get_cascade_readiness(CREATOR)
        return ready, total

    ready, total = await _flags()
    assert ready == 0, (ready, total)
    async with database._connect() as conn:
        cursor = await conn.execute(
            "SELECT id, is_ready_for_next FROM users WHERE session_id = ?",
            (CREATOR,),
        )
        rows = await cursor.fetchall()
    assert all(row[1] == 0 for row in rows), rows


@pytest.mark.asyncio
async def test_claim_requires_both_ready(fresh_db):
    """Claim не срабатывает, если готов только один партнёр."""
    await database.set_ready_for_next(PARTNER, False)
    assert await database.claim_lake_advance(
        CREATOR, "CASCADE_1_LAKE", "CASCADE_2_A"
    ) is False
    assert await database.get_current_step(CREATOR) == "CASCADE_1_LAKE"


@pytest.mark.asyncio
async def test_claim_rejects_wrong_expected_step(fresh_db):
    """Claim не срабатывает, если шаг уже не тот (гонка выиграна другим)."""
    assert await database.claim_lake_advance(
        CREATOR, "CASCADE_2_LAKE", "CASCADE_2_A"
    ) is False
    assert await database.get_current_step(CREATOR) == "CASCADE_1_LAKE"
