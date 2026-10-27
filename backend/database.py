"""Асинхронная работа с SQLite (aiosqlite).

Схема MVP — две таблицы:

users
    id          INTEGER PRIMARY KEY  — Telegram ID пользователя
    name        TEXT                 — имя (first_name) для транскрипта
    session_id  INTEGER              — id активной сессии пользователя;
                                       для создателя сессии (user1) это его
                                       собственный Telegram ID, для партнёра
                                       (user2) — ID создавшего сессию.
                                       NULL — пользователь вне сессии.

messages
    id             INTEGER PRIMARY KEY AUTOINCREMENT
    session_id     INTEGER NOT NULL  — к какой сессии относится сообщение
    sender_id      INTEGER NOT NULL  — кто отправил
    original_text  TEXT NOT NULL     — исходный текст
    rephrased_text TEXT NOT NULL     — переформулировка от нейросети

Сессией служит Telegram ID создателя (user1) — отдельная таблица сессий
не нужна. Файл БД лежит рядом с кодом бота (backend/mediator.db).
"""

import os

import aiosqlite

import os
import time
from contextlib import asynccontextmanager

import aiosqlite

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mediator.db")


@asynccontextmanager
async def _connect():
    """H6: соединение с per-connection прагмой busy_timeout.

    busy_timeout действует ТОЛЬКО на текущий коннект, поэтому задаём
    его в каждом соединении; WAL ('journal_mode') сохраняется в самом
    файле базы — его достаточно один раз задать в init_db.

    Использование: `async with _connect() as conn: ...` — контекстный
    менеджер сам закроет соединение (и его поток) после выхода.
    """
    conn = await aiosqlite.connect(DB_FILE)
    try:
        await conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        await conn.close()


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет.

    H6: PRAGMA journal_mode=WAL — читатели не блокируются читателями;
    PRAGMA busy_timeout=5000 — вместо мгновенного 'database is locked'
    операция подождёт до 5 секунд, пока освободится база.
    """
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY,
                name       TEXT,
                session_id INTEGER,
                gender     TEXT,
                about      TEXT
            )
            """
        )
        # Онбординг: миграция существующей базы (ALTER не умеет IF NOT EXISTS
        # для колонок, поэтому проверяем PRAGMA table_info)
        existing = await conn.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in await existing.fetchall()}
        if "gender" not in columns:
            await conn.execute("ALTER TABLE users ADD COLUMN gender TEXT")
        if "about" not in columns:
            await conn.execute("ALTER TABLE users ADD COLUMN about TEXT")
        # «Водопад» (EFT): флаг «нажал кнопку Дальше» — индивидуальный,
        # поэтому хранится на самом пользователе
        if "is_ready_for_next" not in columns:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN is_ready_for_next INTEGER DEFAULT 0"
            )
        # «Водопад»: состояние ступени сессии — общее для обоих партнёров,
        # хранится в отдельной таблице (одна строка на сессию)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   INTEGER PRIMARY KEY,
                current_step TEXT NOT NULL DEFAULT 'CASCADE_1_A',
                started_at   REAL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id     INTEGER NOT NULL,
                sender_id      INTEGER NOT NULL,
                original_text  TEXT NOT NULL,
                rephrased_text TEXT NOT NULL
            )
            """
        )
        # S4: индекс под частый запрос /api/history (иначе — full scan
        # всей таблицы на каждый тик опроса)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)"
        )
        # S2: блокировка «сессия завершается» — атомарная защита от
        # двойного /finish (PRIMARY KEY делает второй INSERT невозможным)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS finish_locks (
                session_id INTEGER PRIMARY KEY
            )
            """
        )
        # S3: единственный слот партнёра сессии — атомарная защита от
        # одновременного подключения двоих по одной invite-ссылке
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_partners (
                session_id INTEGER PRIMARY KEY,
                partner_id INTEGER NOT NULL
            )
            """
        )
        await conn.commit()


async def try_lock_finish(session_id: int) -> bool:
    """S2: атомарно занимает сессию под генерацию резюме.

    Возвращает True, если блокировка получена (мы — первый завершающий),
    False — если резюме уже генерируется/сессия завершена в другом
    обработчике (бот или сайт, возможны и разные процессы).
    """
    async with _connect() as conn:
        try:
            await conn.execute(
                "INSERT INTO finish_locks (session_id) VALUES (?)", (session_id,)
            )
        except aiosqlite.IntegrityError:
            return False
        await conn.commit()
        return True


async def release_finish_lock(session_id: int) -> None:
    """S2: снимает блокировку (после завершения ИЛИ сбоя генерации)."""
    async with _connect() as conn:
        await conn.execute(
            "DELETE FROM finish_locks WHERE session_id = ?", (session_id,)
        )
        await conn.commit()


async def try_join_session(
    user_id: int, name: str, session_id: int
) -> bool:
    """S3: атомарное подключение партнёра к сессии.

    Занимает единственный слот партнёра (INSERT с PRIMARY KEY по
    session_id) и сохраняет пользователя ОДНОЙ транзакцией. Если слот
    уже занят (двое пришли по ссылке одновременно) — IntegrityError,
    транзакция роняется целиком, второй получает False.
    """
    async with _connect() as conn:
        try:
            await conn.execute(
                "INSERT INTO session_partners (session_id, partner_id) VALUES (?, ?)",
                (session_id, user_id),
            )
            await conn.execute(
                """
                INSERT INTO users (id, name, session_id) VALUES (?, ?, ?)
                ON CONFLICT(id)
                DO UPDATE SET name = excluded.name,
                              session_id = excluded.session_id
                """,
                (user_id, name, session_id),
            )
        except aiosqlite.IntegrityError:
            return False
        await conn.commit()
        return True


async def upsert_user(user_id: int, name: str, session_id: int | None) -> None:
    """Создаёт или обновляет пользователя и его привязку к сессии."""
    async with _connect() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, name, session_id) VALUES (?, ?, ?)
            ON CONFLICT(id)
            DO UPDATE SET name = excluded.name, session_id = excluded.session_id
            """,
            (user_id, name, session_id),
        )
        await conn.commit()


async def get_user_session(user_id: int) -> int | None:
    """Возвращает id активной сессии пользователя или None."""
    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT session_id FROM users WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_user_info(user_id: int) -> dict | None:
    """Профиль пользователя для промпта медиатора: {name, gender, about}.

    gender/'М'|'Ж'|None, about — суть конфликта из онбординга (или None).
    None — пользователя нет в базе.
    """
    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT name, gender, about FROM users WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"name": row[0], "gender": row[1], "about": row[2]}


async def update_profile(user_id: int, name: str, gender: str, about: str) -> None:
    """Сохраняет результаты онбординга (имя, пол, суть конфликта)."""
    async with _connect() as conn:
        await conn.execute(
            "UPDATE users SET name = ?, gender = ?, about = ? WHERE id = ?",
            (name, gender, about, user_id),
        )
        await conn.commit()


async def get_session_partner(session_id: int, user_id: int) -> int | None:
    """Telegram ID второго участника сессии или None, если партнёра нет."""
    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT id FROM users WHERE session_id = ? AND id != ?",
            (session_id, user_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def add_message(
    session_id: int,
    sender_id: int,
    original_text: str,
    rephrased_text: str,
) -> int:
    """Сохраняет пару "оригинал + переформулировка" и возвращает id записи.

    id нужен фронтенду: чтобы Mini App мог пометить только что отправленное
    сообщение как "уже показанное" и не рисовать его второй раз,
    когда оно придёт из опроса /api/history.
    """
    async with _connect() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO messages (session_id, sender_id, original_text, rephrased_text)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, sender_id, original_text, rephrased_text),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_session_messages(session_id: int) -> list[tuple[int, str, str]]:
    """Все сообщения сессии по порядку: (sender_id, original, rephrased)."""
    async with _connect() as conn:
        cursor = await conn.execute(
            """
            SELECT sender_id, original_text, rephrased_text
            FROM messages
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1], row[2]) for row in rows]


async def get_last_message_by_sender(
    session_id: int, sender_id: int
) -> tuple[int, str, str] | None:
    """Последнее СОБСТВЕННОЕ сообщение пользователя в сессии.

    Нужен кнопке «Раскрыть подробнее»: совет строится вокруг того,
    что пользователь последний раз написал партнёру.
    (sender_id, original_text, rephrased_text) или None, если ещё не писал.
    """
    async with _connect() as conn:
        cursor = await conn.execute(
            """
            SELECT sender_id, original_text, rephrased_text
            FROM messages
            WHERE session_id = ? AND sender_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id, sender_id),
        )
        row = await cursor.fetchone()
        return (row[0], row[1], row[2]) if row else None


async def get_session_messages_for_api(
    session_id: int, after_id: int = 0
) -> list[dict]:
    """Сообщения сессии для фронтенда: [{"id", "sender_id", "rephrased"}, ...].

    Отдаём только переформулированные тексты (original_text остаётся в базе —
    злые слова Mini App не показывает) и sender_id: фронтенд сравнивает
    его со своим текущим пользователем, чтобы красить СВОИ пузыри синим,
    а сообщения партнёра — серым.

    S4: after_id — курсор инкрементальной истории: при after_id > 0
    возвращаются только сообщения с id БОЛЬШЕ курсора (новенькие),
    при after_id = 0 (default) — вся история сессии.
    """
    async with _connect() as conn:
        if after_id > 0:
            cursor = await conn.execute(
                """
                SELECT id, sender_id, rephrased_text
                FROM messages
                WHERE session_id = ? AND id > ?
                ORDER BY id
                """,
                (session_id, after_id),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT id, sender_id, rephrased_text
                FROM messages
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            )
        rows = await cursor.fetchall()
        return [
            {"id": row[0], "sender_id": row[1], "rephrased": row[2]}
            for row in rows
        ]


async def delete_session(session_id: int) -> None:
    """Завершает сессию: удаляет сообщения, отвязывает участников,
    освобождает слот партнёра, блокировку finish и состояние «Водопада»."""
    async with _connect() as conn:
        await conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await conn.execute(
            "UPDATE users SET session_id = NULL, is_ready_for_next = 0 "
            "WHERE session_id = ?",
            (session_id,),
        )
        await conn.execute(
            "DELETE FROM session_partners WHERE session_id = ?", (session_id,)
        )
        await conn.execute(
            "DELETE FROM finish_locks WHERE session_id = ?", (session_id,)
        )
        # «Водопад»: состояние ступени этой сессии тоже стирается
        await conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        await conn.commit()


async def get_current_step(session_id: int) -> str | None:
    """Ступень «Водопада» для сессии или None, если он ещё не запущен."""
    async with _connect() as conn:
        cursor = await conn.execute(
            "SELECT current_step FROM sessions WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_current_step(session_id: int, step: str) -> None:
    """Сохраняет (или перезапускает) ступень «Водопада» для сессии."""
    async with _connect() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (session_id, current_step, started_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE
            SET current_step = excluded.current_step
            """,
            (session_id, step, time.time()),
        )
        await conn.commit()


async def set_ready_for_next(user_id: int, value: bool) -> None:
    """Отмечает нажатие кнопки «🌊 Дальше» конкретным участником."""
    async with _connect() as conn:
        await conn.execute(
            "UPDATE users SET is_ready_for_next = ? WHERE id = ?",
            (1 if value else 0, user_id),
        )
        await conn.commit()


async def get_cascade_readiness(session_id: int) -> tuple[int, int]:
    """Сколько участников сессии нажали «Дальше» (из двух возможных)."""
    async with _connect() as conn:
        cursor = await conn.execute(
            """
            SELECT COUNT(*) FROM users
            WHERE session_id = ? AND is_ready_for_next = 1
            """,
            (session_id,),
        )
        (ready,) = await cursor.fetchone()
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM users WHERE session_id = ?", (session_id,)
        )
        (total,) = await cursor.fetchone()
        return ready, total
