"""Точка входа: Telegram-бот "Семейный медиатор".

Партнёры объединяются в сессию по ссылке-приглашению. Бот переформулирует
каждое сообщение в мягкое "Я-сообщение" и отправляет его СОБЕСЕДНИКУ.
По команде /finish DeepSeek анализирует всю переписку и выдаёт обоим
краткое резюме конфликта.

Сессии и история сообщений хранятся в SQLite (backend/database.py).
Сессией служит Telegram ID создателя (user1); партнёр (user2) получает
тот же session_id при подключении по ссылке.
"""

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

# Как и в api.py: каталог backend/ в sys.path при запуске `python backend/main.py`
# Python добавляет сам, при запуске как модуля пакета — нет. Страхуемся.
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import api
import database
import llm
import uvicorn

# Без этой строки aiogram молчит: сообщения "Run polling for bot..." идут
# на уровне INFO, а Python по умолчанию показывает только WARNING и выше —
# из-за этого казалось, что бот не запускается, хотя polling работал.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

INVITE_PREFIX = "invite_"
# H3: защита от спама — не чаще одного сообщения каждые RATE_LIMIT_SECONDS.
# Словарь общий на процесс: ключ — Telegram ID, значение — time.monotonic()
# последнего ПРОЩЁДШЕГО сообщения (отвергнутые попытки не отсчитывают окно заново:
# спамер долбится не чаще 1 запроса в нейросеть за 2 секунды).
RATE_LIMIT_SECONDS = 2
last_message_time: dict[int, float] = {}

# Username бота заполняется при запуске (для генерации ссылок-приглашений)
bot_username = ""

# ── Онбординг (знакомство перед диалогом) ────────────────────────────
# Словарь в памяти: user_id -> {"step": "name"|"about",
# "name": str, "gender": "М"|"Ж"|None, "session_id": int, "is_creator": bool}.
# Словарь для двух вопросов избыточен; при рестарте процесса анкета
# сбрасывается — пользователь просто продолжит её по команде /start.
# Пол выбирается КНОПКАМИ (callback_data gender_male/gender_female),
# текстом вводится только имя — это исключает ошибки парсинга.
onboarding: dict[int, dict] = {}

# Два вопроса онбординга
Q_NAME = (
    "Как к вам обращаться и какого вы пола?\n"
    "Напишите имя одним сообщением, а пол выберите кнопками ниже 👇"
)
Q_ABOUT = "Опишите в двух словах суть конфликта, чтобы я мог лучше вас понять."

# Токены пола по границам слов: если пользователь всё же напишет пол
# текстом рядом с именем ("Антон, мужчина"), имя выкусим без слова пола.
GENDER_M_RE = re.compile(r"\b(мужчина|муж|парень|м)\b", re.IGNORECASE)
GENDER_F_RE = re.compile(r"\b(женщина|жен|девушка|ж|дев)\b", re.IGNORECASE)


GENDER_LABELS = {"М": "Мужчина", "Ж": "Женщина"}


def _gender_keyboard() -> InlineKeyboardMarkup:
    """Inline-кнопки выбора пола для онбординга."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="👨 Мужчина", callback_data="gender_male"),
            InlineKeyboardButton(text="👩 Женщина", callback_data="gender_female"),
        ]]
    )


def _change_gender_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «Изменить» на сообщении-подтверждении выбора пола."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Изменить", callback_data="change_gender")]]
    )


async def _send_name_question(send, intro: str | None = None) -> None:
    """Вопрос об имени + Inline-клавиатура выбора пола одной посылкой.

    send — message.answer или callback.message.answer.
    """
    if intro:
        await send(intro)
    await send(Q_NAME, reply_markup=_gender_keyboard())


def extract_name(text: str, fallback: str) -> str:
    """Имя из ответа онбординга ('Антон' -> 'Антон').

    Если пользователь по привычке написал и пол ('Антон, мужчина'),
    гендерные слова удаляем; дальше разбираем по запятым и берём самый
    длинный осмысленный кусок; при пустом или слишком длинном результате —
    fallback (first_name из Telegram).
    """
    cleaned = GENDER_M_RE.sub(" ", text)
    cleaned = GENDER_F_RE.sub(" ", cleaned)
    parts = [p.strip(" \t.,!?—-") for p in cleaned.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return fallback
    name = max(parts, key=len)
    if not name or len(name) > 64:
        return fallback
    return name


def _begin_onboarding(user_id: int, session_id: int, is_creator: bool) -> None:
    """Начинает (или перезапускает) анкету знакомства."""
    onboarding[user_id] = {
        "step": "name",
        "name": "",
        "gender": None,
        "session_id": session_id,
        "is_creator": is_creator,
    }


async def _onboarding_respond(send, user_id: int) -> None:
    """Проверяет анкету после каждого шага и решает, что спросить дальше.

    Имя и пол могут прийти в любом порядке (текстом или кнопкой) —
    движемся к вопросу о сути конфликта только когда есть оба.
    """
    state = onboarding.get(user_id)
    if state is None:
        return

    if state["name"] and state["gender"]:
        state["step"] = "about"
        await send(Q_ABOUT)
    elif state["name"] and state["gender"] is None:
        # Пол ещё не выбран: прилагаем кнопки прямо к этому сообщению
        await send(
            f"Приятно познакомиться, {state['name']}! "
            "А теперь выберите пол:",
            reply_markup=_gender_keyboard(),
        )
    # имя пустое, пол выбран — молча ждём текст с именем


async def _onboarding_step(message: Message, text: str) -> None:
    """Обрабатывает текстовые ответы онбординга (имя, суть конфликта)."""
    user_id = message.from_user.id
    state = onboarding.get(user_id)
    if state is None:  # на всякий случай: анкету сбросили между сообщениями
        return

    if state["step"] == "name":
        fallback = message.from_user.first_name or "партнёр"
        state["name"] = extract_name(text, fallback)
        await _onboarding_respond(message.answer, user_id)

    elif state["step"] == "about":
        if not state["gender"]:
            # Пол сброшен кнопкой «🔄 Изменить» после отправки Q_ABOUT:
            # сначала возвращаем выбор пола, профиль пока не трогаем
            state["step"] = "name"
            await message.answer(
                "Сначала выберите пол:", reply_markup=_gender_keyboard()
            )
            return
        about = text.strip()[:2000]
        # H5: сбой БД не оставляем молчанием
        try:
            await database.update_profile(
                user_id, state["name"], state["gender"], about
            )
        except Exception as e:  # noqa: BLE001
            print(f"ERROR (database.update_profile, онбординг {user_id}): {e!r}")
            await message.answer(
                "Техническая проблема, пожалуйста, повторите попытку через минуту"
            )
            return
        # Анкета заполнена — выходим из онбординга и отпускаем пользователя
        onboarding.pop(user_id, None)

        if state["is_creator"]:
            link = f"https://t.me/{bot_username}?start=invite_{state['session_id']}"
            await message.answer(
                "Спасибо, теперь я знаю, как к вам обращаться!\n\n"
                "Отправьте партнёру эту ссылку — он подключится к сессии:\n"
                f"{link}"
            )
        else:
            await message.answer(
                "Спасибо! Вы подключены друг к другу. Начинайте диалог — "
                "пишите мне сообщения здесь или используйте Mini App."
            )
            # Уведомление создателю сессии: партнёр готов к диалогу
            try:
                await bot.send_message(
                    state["session_id"],
                    "🟢 Ваш партнер прошел знакомство и готов к диалогу. Начинайте!",
                )
            except Exception as e:  # noqa: BLE001 — не критично
                print(f"ERROR (уведомление создателю о готовности, {user_id}): {e!r}")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def command_start_handler(message: Message, command: CommandObject) -> None:
    """
    /start без аргумента  — создаём сессию и выдаём ссылку-приглашение.
    /start invite_<id>    — пользователь по ссылке подключается как user2.
    """
    user_id = message.from_user.id
    user_name = message.from_user.first_name or ""

    # --- пользователь пришёл по ссылке-приглашению ---
    if command.args and command.args.startswith(INVITE_PREFIX):
        try:
            creator_id = int(command.args[len(INVITE_PREFIX):])
        except ValueError:
            await message.answer("Ссылка повреждена. Попросите партнёра прислать её снова.")
            return

        # Сессия существует, только если создатель находится в своей сессии
        if await database.get_user_session(creator_id) != creator_id:
            await message.answer("Приглашение больше не действует. Партнёру нужно отправить /start.")
            return

        if user_id == creator_id:
            link = f"https://t.me/{bot_username}?start=invite_{creator_id}"
            await message.answer(f"Это ваша собственная ссылка — отправьте её партнёру:\n{link}")
        else:
            # Уже подключённый партнёр перешёл по ссылке повторно:
            # если профиль не заполнен — продолжаем онбординг
            if await database.get_user_session(user_id) == creator_id:
                info = await database.get_user_info(user_id)
                if info and not info.get("gender"):
                    _begin_onboarding(user_id, creator_id, is_creator=False)
                    await _send_name_question(message.answer, "Давайте продолжим знакомство:")
                else:
                    await message.answer(
                        "Вы уже подключены к партнёру. Начинайте диалог — "
                        "завершить можно командой /finish"
                    )
                return

            # S3: атомарный захват единственного слота партнёра.
            # Двое пришедшие по ссылке одновременно: второй IntegrityError
            # внутри try_join_session -> получит "уже подключился партнёр".
            try:
                joined = await database.try_join_session(user_id, user_name, creator_id)
            except Exception as e:  # noqa: BLE001 — H5: не молчим при сбое БД
                print(f"ERROR (database.try_join_session, /start invite): {e!r}")
                await message.answer(
                    "Техническая проблема, пожалуйста, повторите попытку через минуту"
                )
                return
            if joined:
                # Онбординг партнёра: знакомимся, прежде чем говорить
                _begin_onboarding(user_id, creator_id, is_creator=False)
                await bot.send_message(
                    creator_id, "Партнёр подключился. Теперь он проходит знакомство"
                )
                await _send_name_question(
                    message.answer, "Вы подключены к сессии! Но сначала познакомимся:"
                )
            else:
                await message.answer("К этой сессии уже подключился партнёр")
        return

    # --- обычный /start ---
    session_id = await database.get_user_session(user_id)
    if session_id is not None:
        # Онбординг: профиль ещё не заполнен (анкету прервали /start'ом) —
        # продолжаем её в том же состоянии создателя/партнёра
        info = await database.get_user_info(user_id)
        if info is not None and not info.get("gender"):
            _begin_onboarding(user_id, session_id, is_creator=(session_id == user_id))
            await _send_name_question(message.answer, "Давайте продолжим знакомство:")
            return

        # Мёртвая сессия: в ней нет ни одного сообщения. Она держит
        # пользователя в «диалоге», не давая начать заново, — чистим и
        # проваливаемся ниже, чтобы /start сразу создал новую сессию.
        messages = await database.get_session_messages(session_id)
        if not messages:
            partner_id = await database.get_session_partner(session_id, user_id)
            try:
                await database.delete_session(session_id)
                await database.update_profile(
                    user_id, (info or {}).get("name") or "", None, None
                )
            except Exception as e:  # noqa: BLE001 — H5: не молчим при сбое БД
                print(f"ERROR (очистка пустой сессии, /start {user_id}): {e!r}")
                await message.answer(
                    "Техническая проблема, пожалуйста, повторите попытку через минуту"
                )
                return
            if partner_id is not None:
                # Партнёр был в этой пустой сессии тоже — честно скажем ему
                try:
                    await bot.send_message(
                        partner_id,
                        "Пустая сессия отменена. Нажмите /start, чтобы начать заново",
                    )
                except Exception as e:  # noqa: BLE001 — уведомление не критично
                    print(f"ERROR (уведомление партнёра о пустой сессии, {user_id}): {e!r}")
            # fall-through: ниже /start создаёт новую сессию и запускает онбординг
        else:
            if session_id == user_id:
                # Пользователь — создатель сессии (user1)
                partner_id = await database.get_session_partner(session_id, user_id)
                link = f"https://t.me/{bot_username}?start=invite_{session_id}"
                if partner_id is not None:
                    await message.answer("Вы уже в диалоге с партнёром. Завершить — команда /finish")
                else:
                    await message.answer(f"Вы в ожидании партнёра. Его ссылка:\n{link}")
            else:
                await message.answer("Вы уже подключены к партнёру. Завершить — команда /finish")
            return

    # Новая сессия: id сессии = Telegram ID создателя
    try:
        await database.upsert_user(user_id, user_name, user_id)
    except Exception as e:  # noqa: BLE001 — H5: не молчим при сбое БД
        print(f"ERROR (database.upsert_user, /start): {e!r}")
        await message.answer(
            "Техническая проблема, пожалуйста, повторите попытку через минуту"
        )
        return
    # Онбординг: знакомимся, ссылку даём только после анкеты
    _begin_onboarding(user_id, user_id, is_creator=True)
    await _send_name_question(
        message.answer,
        "Привет! Я медиатор. Помогу вам разговаривать бережно: "
        "я переформулирую ваши слова в мягкие 'Я-сообщения' "
        "и передам их партнёру.\n\n"
        "Сначала давайте знакомиться:",
    )


@dp.message(Command("finish"))
async def finish_handler(message: Message) -> None:
    """/finish: отправляем всю историю сессии в DeepSeek и раздаём резюме обоим."""
    user_id = message.from_user.id
    session_id = await database.get_user_session(user_id)
    if session_id is None:
        await message.answer("У вас нет активной сессии. Нажмите /start")
        return

    partner_id = await database.get_session_partner(session_id, user_id)
    if partner_id is None:
        # Партнёр ещё не подключился: резюмировать нечего. Вместо тупикового
        # «Ожидаем вашего партнера...» даём односторонний выход — сессия
        # полностью удаляется, профиль сбрасывается, можно сразу начать заново.
        try:
            await database.delete_session(session_id)
            info = await database.get_user_info(user_id)
            await database.update_profile(
                user_id, (info or {}).get("name") or "", None, None
            )
        except Exception as e:  # noqa: BLE001 — H5: не молчим при сбое БД
            print(f"ERROR (отмена сессии, /finish {user_id}): {e!r}")
            await message.answer(
                "Техническая проблема, пожалуйста, повторите попытку через минуту"
            )
            return
        await message.answer("Сессия отменена. Вы можете начать заново командой /start")
        return

    messages = await database.get_session_messages(session_id)
    if not messages:
        # Сессия «висит» без единого сообщения: держит пользователя в
        # диалоге, не давая начать заново. Отменяем её сразу.
        try:
            await database.delete_session(session_id)
            info = await database.get_user_info(user_id)
            await database.update_profile(
                user_id, (info or {}).get("name") or "", None, None
            )
        except Exception as e:  # noqa: BLE001 — H5: не молчим при сбое БД
            print(f"ERROR (отмена пустой сессии, /finish {user_id}): {e!r}")
            await message.answer(
                "Техническая проблема, пожалуйста, повторите попытку через минуту"
            )
            return
        await message.answer(
            "Пустая сессия отменена. Вы можете начать заново командой /start"
        )
        return

    # S2: атомарная блокировка от двойного /finish (одновременно нажали
    # бот и Mini App, или два разных устройства). Второй получит отказ.
    try:
        locked = await database.try_lock_finish(session_id)
    except Exception as e:  # noqa: BLE001 — H5: не молчим при сбое БД
        print(f"ERROR (database.try_lock_finish, /finish): {e!r}")
        await message.answer(
            "Техническая проблема, пожалуйста, повторите попытку через минуту"
        )
        return
    if not locked:
        await message.answer("Сессия уже завершена или не существует")
        return

    try:
        # Транскрипт: кто что сказал и как это было переформулировано
        lines = []
        for sender_id, original, rephrased in messages:
            side = "Партнёр 1" if sender_id == session_id else "Партнёр 2"
            lines.append(f"{side}: {original} (переформулировано: {rephrased})")
        transcript = "\n".join(lines)

        try:
            summary = await llm.summarize_transcript(transcript)
        except Exception:
            await message.answer("Извини, нейросеть сейчас недоступна")
            return

        await message.answer("Резюме сессии:\n\n" + summary)
        try:
            await bot.send_message(partner_id, "Резюме сессии:\n\n" + summary)
        except Exception:
            await message.answer("Не удалось доставить резюме партнёру.")

        # Сессия завершена: чистим сообщения и привязки участников
        # (delete_session заодно снимает блокировку finish)
        await database.delete_session(session_id)
    finally:
        # Страховка: если что-то упало до delete_session, блокировку
        # снимаем вручную, чтобы сессию можно было завершить повторно
        await database.release_finish_lock(session_id)


@dp.message(Command("myid"))
async def myid_handler(message: Message) -> None:
    """/myid: присылает пользователю его реальный Telegram ID.

    Нужен для тестов: этот ID можно захардкодить в frontend/index.html
    (константа TEST_USER_ID), чтобы отправлять сообщения из браузера
    в Telegram без запуска Mini App внутри Telegram.
    """
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message()
async def rephrase_message_handler(message: Message) -> None:
    """Текстовое сообщение: переформулировать и отправить СОБЕСЕДНИКУ."""
    user_id = message.from_user.id
    text = message.text
    if text is None:
        return  # фото, стикеры и прочее не обрабатываем в MVP

    # Онбординг перехватывает поток ПЕРВЫМ: пока анкета не заполнена,
    # обычные сообщения (проверки сессии/лимита/безопасности) не работают
    if user_id in onboarding:
        await _onboarding_step(message, text)
        return

    session_id = await database.get_user_session(user_id)
    if session_id is None:
        await message.answer("Нажмите /start, чтобы создать сессию и позвать партнёра")
        return

    partner_id = await database.get_session_partner(session_id, user_id)
    if partner_id is None:
        await message.answer("Ожидаем вашего партнера...")
        return

    # S7: серверный лимит длины — не отправляем в нейросеть, что и дорого,
    # и бессмысленно; фронтенде maxlength=1000, а боту Telegram даёт 4096.
    if len(text) > 2000:
        await message.answer(
            "Сообщение слишком длинное. Пожалуйста, сократите его до 2000 символов"
        )
        return

    # H3: rate-limit — долбящий пользователь не доходит до DeepSeek
    now = time.monotonic()
    last = last_message_time.get(user_id)
    if last is not None and now - last < RATE_LIMIT_SECONDS:
        await message.answer("Пожалуйста, не так быстро. Подождите пару секунд")
        return
    # Метку ставим при прохождении лимита (до нейросети): пока идёт запрос
    # DeepSeek, повторное сообщение уже отсчитывает новое окно
    last_message_time[user_id] = now

    # Проверка безопасности: реальная угроза — не повод для медиации,
    # а повод остановиться. Такое сообщение не уходит партнёру.
    is_dangerous, reason = await llm.check_safety(text)
    if is_dangerous:
        logging.info(
            "Сообщение от %s заблокировано проверкой безопасности: %s",
            user_id, reason,
        )
        await message.answer(llm.SAFETY_RESPONSE)
        return

    # Онбординг-контекст: имена и пол обеих сторон — чтобы нейросеть не
    # путала, кто есть кто. Профиль может отсутствовать (None) — llm сам
    # соберёт промпт без пола.
    sender_info = await database.get_user_info(user_id)
    partner_info = await database.get_user_info(partner_id)

    try:
        rephrased = await llm.rephrase_text(
            text,
            sender_name=(sender_info or {}).get("name") or "пользователь",
            sender_gender=(sender_info or {}).get("gender"),
            partner_name=((partner_info or {}).get("name") or "партнёр"),
            partner_gender=(partner_info or {}).get("gender"),
            context=(sender_info or {}).get("about") or "",
        )
    except Exception:
        # Любая ошибка API (сеть, неверный ключ, лимиты) не должна ронять бота
        await message.answer("Извини, нейросеть сейчас недоступна")
        return

    # Пара (оригинал + переформулировка) сохраняется в БД.
    # H5: если база временно занята/недоступна — не молчим, а говорим
    # пользователю; переформулировка в этом случае теряется.
    try:
        await database.add_message(session_id, user_id, text, rephrased)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR (database.add_message, текст от {user_id}): {e!r}")
        await message.answer(
            "Техническая проблема, пожалуйста, повторите попытку через минуту"
        )
        return

    # Результат уходит собеседнику, а не отправителю
    try:
        await bot.send_message(partner_id, rephrased)
    except Exception:
        await message.answer(
            "Партнёру не удалось доставить сообщение. Скорее всего, он заблокировал бота."
        )

    # «Раскрыть подробнее»: короткие сообщения не всегда говорят всё.
    # Следом за доставкой отправляем отправителю маленькую подсказку
    # с Inline-кнопкой — подробный совет получит только он.
    try:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="Раскрыть подробнее",
                    callback_data="expand_context",
                )
            ]]
        )
        await message.answer(
            "💡 Хочешь, я помогу сформулировать, что именно тебе нужно "
            "сказать партнёру?",
            reply_markup=keyboard,
        )
    except Exception as e:  # noqa: BLE001 — подсказка не критична
        print(f"ERROR (inline keyboard, отправка подсказки {user_id}): {e!r}")


@dp.callback_query(F.data.in_({"gender_male", "gender_female"}))
async def gender_choice_handler(callback: CallbackQuery) -> None:
    """Кнопки выбора пола в онбординге: сохраняем пол и двигаем анкету.

    Пол может быть выбран ДО ввода имени или после — оба порядка валидны;
    следующий вопрос задаётся только когда собраны оба ответа.
    """
    user_id = callback.from_user.id
    state = onboarding.get(user_id)

    async def acknowledge(text: str = "") -> None:
        # Снимаем "часики" на кнопке; с попутным уведомлением — если есть что сказать
        try:
            if text:
                await callback.answer(text, show_alert=False)
            else:
                await callback.answer()
        except Exception:  # noqa: BLE001 — ответ на callback не критичен
            pass

    if state is None:
        await acknowledge("Анкета уже завершена")
        return

    gender = "М" if callback.data == "gender_male" else "Ж"
    # Кнопки после выбора убираются edit_text'ом, поэтому повторное нажатие
    # возможно только на «зависшем» старом сообщении — молча переиграем его
    state["gender"] = gender
    await acknowledge()

    if callback.message is None:  # очень старое сообщение без контекста
        return

    # 1) Сообщение с кнопками превращаем в подтверждение (кнопки убраны),
    #    а выбор пола оставляем доступным через «🔄 Изменить»
    confirmation = f"✅ Пол выбран: {GENDER_LABELS[gender]}"
    try:
        await callback.message.edit_text(
            confirmation, reply_markup=_change_gender_keyboard()
        )
    except Exception as e:  # noqa: BLE001 — edit может не пройти (старое msg)
        print(f"ERROR (edit_text, выбор пола {user_id}): {e!r}")
        try:
            await callback.message.answer(
                confirmation, reply_markup=_change_gender_keyboard()
            )
        except Exception as e2:  # noqa: BLE001 — не критично
            print(f"ERROR (подтверждение пола, {user_id}): {e2!r}")

    # 2) Двигаем анкету: имя уже есть — сразу к сути конфликта, нет — просим имя
    if state["name"]:
        state["step"] = "about"
        await callback.message.answer(Q_ABOUT)
    else:
        state["step"] = "name"
        await callback.message.answer(
            "Как к вам обращаться?\nНапишите имя одним сообщением."
        )


@dp.callback_query(F.data == "change_gender")
async def change_gender_handler(callback: CallbackQuery) -> None:
    """🔄 Изменить: возвращаем выбор пола и сбрасываем сохранённый ответ."""
    user_id = callback.from_user.id
    state = onboarding.get(user_id)

    async def acknowledge(text: str = "") -> None:
        try:
            if text:
                await callback.answer(text, show_alert=False)
            else:
                await callback.answer()
        except Exception:  # noqa: BLE001 — ответ на callback не критичен
            pass

    if state is None:
        await acknowledge("Анкета уже завершена")
        return

    # Пол хранится только в памяти онбординга: в БД он попадает лишь после
    # завершения анкеты (update_profile на шаге about), поэтому отдельный
    # сброс в базе здесь не нужен
    state["gender"] = None
    state["step"] = "name"
    await acknowledge("Выберите пол заново")

    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            "Выберите пол:", reply_markup=_gender_keyboard()
        )
    except Exception as e:  # noqa: BLE001
        print(f"ERROR (edit_text, изменение пола {user_id}): {e!r}")
        try:
            await callback.message.answer("Выберите пол:", reply_markup=_gender_keyboard())
        except Exception as e2:  # noqa: BLE001 — не критично
            print(f"ERROR (повторный выбор пола, {user_id}): {e2!r}")


@dp.callback_query(F.data == "expand_context")
async def expand_context_handler(callback: CallbackQuery) -> None:
    """Кнопка «Раскрыть подробнее»: развёрнутый совет только нажавшему.

    Берём последнее СОБСТВЕННОЕ сообщение пользователя (оригинал +
    мягкая переформулировка) и просим DeepSeek сформулировать, что именно
    стоит сказать партнёру: чувство, потребность, конкретную просьбу.
    Ответ уходит ПЕРСОНАЛЬНО в чат с нажавшим (callback.message.answer).
    """
    user_id = callback.from_user.id

    # Снимаем "часики" на кнопке (не критично, если не успели)
    try:
        await callback.answer()
    except Exception:  # noqa: BLE001
        pass

    session_id = await database.get_user_session(user_id)
    if session_id is None:
        await callback.message.answer("Нажмите /start, чтобы создать сессию")
        return

    last = await database.get_last_message_by_sender(session_id, user_id)
    if last is None:
        await callback.message.answer(
            "Я не нашёл ваших сообщений в этой сессии — напишите что-нибудь первым"
        )
        return
    _, original_text, rephrased_text = last

    # H3: тот же лимит, что и на обычные сообщения — долбить кнопку не дадим
    now = time.monotonic()
    last_ts = last_message_time.get(user_id)
    if last_ts is not None and now - last_ts < RATE_LIMIT_SECONDS:
        await callback.message.answer("Пожалуйста, не так быстро. Подождите пару секунд")
        return
    last_message_time[user_id] = now

    sender_info = await database.get_user_info(user_id)
    partner_id = await database.get_session_partner(session_id, user_id)
    partner_info = (
        await database.get_user_info(partner_id) if partner_id else None
    )

    try:
        advice = await llm.expand_text(
            original_text,
            rephrased_text=rephrased_text or "",
            context=(sender_info or {}).get("about") or "",
        )
    except Exception:
        await callback.message.answer("Извини, нейросеть сейчас недоступна")
        return

    try:
        await callback.message.answer(advice)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR (bot.send advice, {user_id}): {e!r}")


async def start_web_server() -> tuple[asyncio.Task, "uvicorn.Server"]:
    """Поднимает FastAPI (отдаёт фронтенд Mini App) в фоне на порту 8000.

    S5: возвращает и задачу, и сам объект uvicorn.Server — чтобы в finally
    gracefully остановить сервер (should_exit), а не убивать задачу
    реактивным cancel().
    """
    server = uvicorn.Server(
        uvicorn.Config(
            api.app,
            host="0.0.0.0",  # слушаем на всех интерфейсах — так Telegram сможет открыть сайт
            port=8000,
            log_level="info",
        )
    )
    return asyncio.create_task(server.serve()), server


async def main() -> None:
    """Запускаем бота в режиме long polling + веб-сервер фронтенда."""
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не найден. Скопируйте .env.example в .env "
            "и укажите токен бота."
        )

    # Создаём таблицы SQLite, если их ещё нет
    await database.init_db()

    # Поднимаем веб-сервер с фронтендом параллельно с ботом.
    # ВАЖНО await: start_web_server — корутина; без await она не выполнится
    # (сервер не стартует), а в finally случится AttributeError
    # ("'coroutine' object has no attribute 'cancel'") при нажатии Ctrl+C.
    web_task, web_server = await start_web_server()
    logging.getLogger("app").info("Фронтенд Mini App: http://127.0.0.1:8000")

    # username нужен для генерации ссылок-приглашений
    global bot_username
    me = await bot.me()
    bot_username = me.username

    # Сбрасываем накопившиеся обновления и начинаем опрос серверов Telegram
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        # S5: Аккуратно гасим веб-сервер при остановке бота (Ctrl+C):
        # просим uvicorn завершиться штатно и дожидаемся задачи.
        # return_exceptions — чтобы исключение фонового сервера не затёрло
        # исходную причину остановки бота.
        web_server.should_exit = True
        await asyncio.gather(web_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
