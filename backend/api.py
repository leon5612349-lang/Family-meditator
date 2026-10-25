"""FastAPI: веб-сервер для Telegram Mini App.

Отдаёт статику (frontend/index.html) и работает с теми же сессиями
и базой данных SQLite, что и Telegram-бот: /api/send переформулирует
текст в "Я-сообщение" и пишет его в историю сессии.

TODO на будущее: при росте проекта — вынести rate-limit и дублируемую
валидацию initData в общие модули (промпты и LLM-клиент уже в llm.py).
"""

import hashlib
import hmac
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

# Гарантируем, что каталог backend/ есть в sys.path в ОБЕИХ схемах запуска:
# - python backend/main.py (Python сам добавил папку скрипта — ничего не происход);
# - uvicorn backend.api:app из корня проекта (рабочая папка — корень, каталог
#   backend/ в sys.path отсутствует, поэтому импорты database/main падали).
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import database
import llm

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Загружаем переменные окружения из файла .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")  # нужен для проверки подписи initData

# H1: initData действителен не дольше суток (рекомендация Telegram) —
# поле auth_date проверяется в validate_init_data
AUTH_MAX_AGE = 86_400

# frontend/ лежит рядом с backend/
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# H3: защита от спама — не чаще одного сообщения каждые RATE_LIMIT_SECONDS.
# Словарь общий на процесс: ключ — Telegram ID, значение — time.monotonic()
# последнего ПРОЩЁДШЕГО сообщения (отвергнутые попытки не отсчитывают окно заново:
# спамер долбится не чаще 1 запроса в нейросеть за 2 секунды).
RATE_LIMIT_SECONDS = 2
last_message_time: dict[int, float] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """При старте сервера создаём таблицы БД, если их ещё нет.

    Важно для запуска uvicorn отдельно от бота (`uvicorn api:app`):
    main() бота вызывает init_db() сам, но api.py должен работать и один.
    """
    await database.init_db()
    yield


app = FastAPI(title="Семейный медиатор", lifespan=lifespan)


class SendRequest(BaseModel):
    """Тело запроса /api/send.

    Из Telegram приходит `{"init_data": "..."}` — подписанная Telegram строка.
    Из браузера (тесты) приходит `{"user_id": <TEST_USER_ID>}` — fallback.
    """

    user_id: int | None = None
    init_data: str = ""
    text: str = ""


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """
    Проверяет подпись initData по стандартному алгоритму Telegram WebApp
    (HMAC-SHA256) и возвращает объект пользователя или None.

    Алгоритм согласно документации Telegram:
    1. secret_key = HMAC-SHA256(key="WebAppData", message=BOT_TOKEN)
    2. data_check_string = все пары "key=value" из initData, кроме "hash",
       отсортированные по алфавиту и склеенные "\\n"
    3. hash = HMAC-SHA256(key=secret_key, message=data_check_string).hexdigest()
       — должен совпасть с полем "hash" из initData.
    4. H1: поле auth_date не старше AUTH_MAX_AGE секунд (24 часа) —
       перехваченная старая строка initData бесполезна.
    """
    if not init_data or not bot_token:
        return None

    try:
        received_hash = None
        raw_pairs = []  # пары в исходном (percent-encoded) виде — их и проверяем
        for pair in init_data.split("&"):
            if pair.startswith("hash="):
                received_hash = pair[len("hash="):]
            else:
                raw_pairs.append(pair)
        if not received_hash or not raw_pairs:
            return None

        data_check_string = "\n".join(sorted(raw_pairs))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None

        # Подпись верна — проверяем свежесть и достаём user
        fields = dict(parse_qsl(init_data, keep_blank_values=True))
        try:
            auth_ts = int(fields.get("auth_date", ""))
        except (TypeError, ValueError):
            return None  # auth_date отсутствует или не число — отклоняем
        if time.time() - auth_ts > AUTH_MAX_AGE:
            return None  # initData старше суток — отклоняем (H1)

        user_raw = fields.get("user", "")
        return json.loads(user_raw) if user_raw else None
    except Exception:  # noqa: BLE001 — битые данные считаем невалидными
        return None


def _fallback_allowed(http_request: Request) -> bool:
    """C2: браузерный fallback (пустой initData) разрешён ТОЛЬКО с localhost.

    Раньше fallback принимал user_id из тела запроса у любого клиента:
    при открытом извне сервере (0.0.0.0) любой мог читать и писать
    в чужие сессии, зная Telegram ID. Теперь fallback работает только
    для локальных тестов; из Telegram всегда приходит подписанный initData.
    """
    client = http_request.client
    if client is None:
        return False
    return client.host in ("127.0.0.1", "::1")


@app.get("/")
async def index() -> FileResponse:
    """Корневой адрес: отдаём HTML Mini App."""
    return FileResponse(FRONTEND_DIR / "index.html", media_type="text/html")


def _get_bot():
    """Возвращает экземпляр Telegram-бота без двойного создания.

    Импорт bot делаем лениво, поскольку main.py импортирует api —
    импорт на верхнем уровне api.py дал бы циркулярную зависимость.
    - python backend/main.py: бот уже работает как __main__.bot — берём его;
    - uvicorn backend.api:app отдельным процессом: импортируем main
      (sys.path уже добавлен выше) и берём main.bot.
    """
    running = sys.modules.get("__main__")
    bot = getattr(running, "bot", None)
    if bot is not None:
        return bot
    import main
    return main.bot


@app.post("/api/send")
async def send(request: SendRequest, http: Request) -> JSONResponse:
    """Переформулирует текст в "Я-сообщение" и сохраняет его в сессию."""

    # 0. Аутентификация: проверяем подпись initData и извлекаем user_id
    tg_user = validate_init_data(request.init_data, BOT_TOKEN)
    if tg_user:
        user_id = tg_user["id"]
    elif request.init_data:
        # initData передан, но подпись НЕ сошлась — подделка или битые данные
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    elif not _fallback_allowed(http):
        # C2: initData пуст (нет подписанного контекста Telegram), но запрос
        # НЕ локальный — браузерный fallback для тестов разрешён только
        # с localhost, остальным Unauthorized
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    else:
        # Fallback для тестов в обычном браузере на localhost.
        # В ПРОДАКШЕНЕ ЭТОЙ ВЕТКИ НЕ БУДЕТ: сайт открывается только
        # внутри Telegram и всегда присылает подписанный initData.
        user_id = request.user_id

    if not user_id:
        return JSONResponse(
            {"error": "Нужен init_data (из Telegram) или user_id (для тестов)"},
            status_code=400,
        )

    # 1. Активная сессия пользователя из общей с ботом базы данных
    session_id = await database.get_user_session(user_id)
    if session_id is None:
        return JSONResponse(
            {"error": "Сначала запустите бота и создайте сессию"},
            status_code=400,
        )

    # 2. Сессия есть, но второй партнёр ещё не подключился
    partner_id = await database.get_session_partner(session_id, user_id)
    if partner_id is None:
        return JSONResponse({"error": "Ожидаем партнера"}, status_code=400)

    # S7: серверный лимит длины — не отправляем в нейросеть, что и дорого,
    # и бессмысленно; фронтенде maxlength=1000, а боту Telegram даёт 4096.
    if len(request.text) > 2000:
        return JSONResponse(
            {"error": "Сообщение слишком длинное. Пожалуйста, сократите его до 2000 символов"},
            status_code=400,
        )

    # 2.4 H3: rate-limit — долбящий пользователь не доходит до DeepSeek.
    # Метку ставим при прохождении лимита (до нейросети): пока идёт запрос
    # DeepSeek, повторное сообщение уже отсчитывает новое окно
    now = time.monotonic()
    last_ts = last_message_time.get(user_id)
    if last_ts is not None and now - last_ts < RATE_LIMIT_SECONDS:
        return JSONResponse(
            {"error": "Пожалуйста, не так быстро. Подождите пару секунд"},
            status_code=429,
        )
    last_message_time[user_id] = now

    # 2.5 Проверка безопасности: реальную угрозу не медиаторим и НЕ
    #     пересылаем партнёру — пользователя остановим прямым текстом.
    is_dangerous, reason = await llm.check_safety(request.text)
    if is_dangerous:
        print(f"[api] сообщение от {user_id} заблокировано проверкой безопасности: {reason}")
        return JSONResponse({"blocked": True, "message": llm.SAFETY_RESPONSE})

    # 3. Переформулировка через DeepSeek
    # Онбординг-контекст: имена и пол обеих сторон из БД — чтобы нейросеть
    # не путала, кто есть кто. Профиль может отсутствовать (None) — llm сам
    # соберёт промпт без пола.
    sender_info = await database.get_user_info(user_id)
    partner_info = await database.get_user_info(partner_id)
    try:
        rephrased = await llm.rephrase_text(
            request.text,
            sender_name=(sender_info or {}).get("name") or "пользователь",
            sender_gender=(sender_info or {}).get("gender"),
            partner_name=((partner_info or {}).get("name") or "партнёр"),
            partner_gender=(partner_info or {}).get("gender"),
            context=(sender_info or {}).get("about") or "",
        )
    except Exception as e:
        # H2: клиенту — человекопонятная фикс-строка, детали остаются в консоли
        print(f"ERROR (DeepSeek, /api/send): {e!r}")
        return JSONResponse(
            {"error": "Извини, нейросеть сейчас недоступна"}, status_code=500
        )

    # 4. Пара (оригинал + переформулировка) в общую историю сессии.
    #    Возвращаем id записи: фронтенд пометит её как уже показанную
    #    и не нарисует второй раз, когда придёт из опроса /api/history.
    message_id = await database.add_message(session_id, user_id, request.text, rephrased)

    # 5. Отправляем переформулировку партнёру в Telegram
    try:
        bot = _get_bot()
        await bot.send_message(chat_id=partner_id, text=rephrased)
    except Exception as e:  # noqa: BLE001 — например, партнёр заблокировал бота
        # Логируем, но не ломаем ответ фронтенду
        print(f"[api] не удалось доставить переформулировку партнёру {partner_id}: {e!r}")

    return JSONResponse({"rephrased": rephrased, "id": message_id})


class ExpandRequest(BaseModel):
    """Тело запроса /api/expand: как у /api/finish, текст не нужен —
    совет строится вокруг последнего сообщения пользователя."""

    user_id: int | None = None
    init_data: str = ""


@app.post("/api/expand")
async def expand(request: ExpandRequest, http: Request) -> JSONResponse:
    """«Раскрыть подробнее» из Mini App: развёрнутый совет нажавшему.

    Зеркалит callback-кнопку в боте: берём последнее СОБСТВЕННОЕ сообщение
    пользователя и просим DeepSeek сформулировать подробную просъбу
    (чувство + потребность + конкретная просьба). Ответ видит только
    запросивший — в общий чат он не попадает.
    """
    tg_user = validate_init_data(request.init_data, BOT_TOKEN)
    if tg_user:
        user_id = tg_user["id"]
    elif request.init_data:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    elif not _fallback_allowed(http):
        # C2: браузерный fallback только для localhost
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    else:
        user_id = request.user_id

    if not user_id:
        return JSONResponse(
            {"error": "Нужен init_data (из Telegram) или user_id (для тестов)"},
            status_code=400,
        )

    session_id = await database.get_user_session(user_id)
    if session_id is None:
        return JSONResponse(
            {"error": "Сначала запустите бота и создайте сессию"},
            status_code=400,
        )

    last = await database.get_last_message_by_sender(session_id, user_id)
    if last is None:
        return JSONResponse(
            {"error": "Пока нет ваших сообщений в сессии — напишите что-нибудь первым"},
            status_code=400,
        )
    _, original_text, rephrased_text = last

    # H3: тот же rate-limit, что и у /api/send (общий словарь) — долбить
    # кнопку быстрее пары секунд нельзя
    now = time.monotonic()
    last_ts = last_message_time.get(user_id)
    if last_ts is not None and now - last_ts < RATE_LIMIT_SECONDS:
        return JSONResponse(
            {"error": "Пожалуйста, не так быстро. Подождите пару секунд"},
            status_code=429,
        )
    last_message_time[user_id] = now

    sender_info = await database.get_user_info(user_id)
    try:
        advice = await llm.expand_text(
            original_text,
            rephrased_text=rephrased_text or "",
            context=(sender_info or {}).get("about") or "",
        )
    except Exception as e:
        # H2: клиенту — человекопонятная фикс-строка, детали остаются в консоли
        print(f"ERROR (DeepSeek, /api/expand): {e!r}")
        return JSONResponse(
            {"error": "Извини, нейросеть сейчас недоступна"}, status_code=500
        )

    return JSONResponse({"advice": advice or ""})


class FinishRequest(BaseModel):
    """Тело запроса /api/finish: как у /api/send, но без текста."""

    user_id: int | None = None
    init_data: str = ""


@app.post("/api/finish")
async def finish(request: FinishRequest, http: Request) -> JSONResponse:
    """Завершает сессию из Mini App: сводит весь диалог в резюме через DeepSeek.

    Логика повторяет /finish у бота в main.py: транскрипт (оригиналы +
    переформулировки, с пометкой "Партнёр 1"/"Партнёр 2"), анализ конфликтa,
    краткое резюме. После успеха сессия завершается и резюме уходит
    партнёру в Telegram (лучшая попытка).
    """
    tg_user = validate_init_data(request.init_data, BOT_TOKEN)
    if tg_user:
        user_id = tg_user["id"]
    elif request.init_data:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    elif not _fallback_allowed(http):
        # C2: браузерный fallback только для localhost
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    else:
        user_id = request.user_id

    if not user_id:
        return JSONResponse(
            {"error": "Нужен init_data (из Telegram) или user_id (для тестов)"},
            status_code=400,
        )

    session_id = await database.get_user_session(user_id)
    if session_id is None:
        return JSONResponse(
            {"error": "Сначала запустите бота и создайте сессию"},
            status_code=400,
        )

    partner_id = await database.get_session_partner(session_id, user_id)
    if partner_id is None:
        # Партнёр ещё не подключился: односторонняя отмена (как /finish у
        # бота) — сессия удаляется, профиль сбрасывается, можно начать заново
        try:
            await database.delete_session(session_id)
            info = await database.get_user_info(user_id)
            await database.update_profile(
                user_id, (info or {}).get("name") or "", None, None
            )
        except Exception as e:  # noqa: BLE001
            print(f"ERROR (отмена сессии, /api/finish {user_id}): {e!r}")
            return JSONResponse(
                {
                    "error": "Техническая проблема, "
                    "пожалуйста, повторите попытку через минуту"
                },
                status_code=500,
            )
        return JSONResponse(
            {
                "cancelled": True,
                "message": "Сессия отменена. Нажмите /start в боте, "
                "чтобы создать новую",
            }
        )

    # Полная история: оригиналы + переформулировки (как у бота)
    messages = await database.get_session_messages(session_id)
    if not messages:
        return JSONResponse(
            {"error": "В сессии пока не было сообщений — анализировать нечего"},
            status_code=400,
        )

    # S2: атомарная блокировка от двойного finish (как у бота: тот же
    # вызов может прилететь и боту, и на сайт, и с двух устройств).
    if not await database.try_lock_finish(session_id):
        return JSONResponse(
            {"error": "Сессия уже завершена или не существует"}, status_code=409
        )

    try:
        # Транскрипт в том же формате, что и у bot /finish
        lines = []
        for sender_id, original, rephrased in messages:
            side = "Партнёр 1" if sender_id == session_id else "Партнёр 2"
            lines.append(f"{side}: {original} (переформулировано: {rephrased})")
        transcript = "\n".join(lines)

        try:
            summary = await llm.summarize_transcript(transcript)
        except Exception as e:
            # H2: клиенту — человекопонятная фикс-строка, детали остаются в консоли
            print(f"ERROR (DeepSeek, /api/finish): {e!r}")
            return JSONResponse(
                {"error": "Извини, нейросеть сейчас недоступна"}, status_code=500
            )

        # Сессия завершена: чистим сообщения и привязки (как /finish у бота;
        # delete_session заодно снимает блокировку finish)
        await database.delete_session(session_id)

        # Резюме партнёру в Telegram — лучшей попыткой, без остановки ответа
        try:
            bot = _get_bot()
            await bot.send_message(partner_id, "Резюме сессии:\n\n" + summary)
        except Exception as e:  # noqa: BLE001
            print(f"[api] не удалось доставить резюме партнёру {partner_id}: {e!r}")

        return JSONResponse({"summary": summary or ""})
    finally:
        # Страховка: если упало до delete_session, снимаем блокировку,
        # чтобы завершение можно было повторить
        await database.release_finish_lock(session_id)


@app.get("/api/history")
async def get_history(
    http: Request,
    after_id: int = 0,
    init_data: str = "",
    user_id: int | None = None,
) -> JSONResponse:
    """История сессии: [{"id": 1, "sender_id": 2, "rephrased": "..."}, ...].

    S4: after_id — инкрементальный курсор (фронт передаёт maxHistoryId):
    0 или опущен — вся история сессии, N — только сообщения с id > N.

    Аутентификация как в /api/send: подпись initData (из Telegram)
    или user_id (fallback только для localhost-тестов в браузере).
    """
    tg_user = validate_init_data(init_data, BOT_TOKEN)
    if tg_user:
        uid = tg_user["id"]
    elif init_data:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    elif not _fallback_allowed(http):
        # C2: браузерный fallback только для localhost
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    else:
        uid = user_id

    if not uid:
        return JSONResponse({"error": "Нужен init_data или user_id"}, status_code=400)

    session_id = await database.get_user_session(uid)
    if session_id is None:
        # Сессии нет — отдаём пустую историю (эндпоинт опрашивается каждые 3 сек,
        # поэтому строгое сообщение об ошибке спамило бы чат UI)...
        info = await database.get_user_info(uid)
        profile_ready = bool(info and info.get("gender"))
        return JSONResponse({"messages": [], "profile_ready": profile_ready})

    # Онбординг: фронт показывает приветственный экран, пока профиль
    # не заполнен (пол — обязательный ответ анкеты)
    info = await database.get_user_info(uid)
    profile_ready = bool(info and info.get("gender"))

    messages = await database.get_session_messages_for_api(
        session_id, after_id=max(0, after_id)
    )
    return JSONResponse({"messages": messages, "profile_ready": profile_ready})


@app.get("/healthz")
async def healthz() -> dict:
    """Простая проверка живости сервера."""
    return {"status": "ok"}
