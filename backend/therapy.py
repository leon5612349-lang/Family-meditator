"""«Водопад» — последовательность каскадов по методу ЭФТ (Сью Джонсон).

Сессия = серия «каскадов». У каждого каскада:
- две «ступени»-вопроса (CASCADE_x_A — вопрос партнёру 1, создателю
  сессии; CASCADE_x_B — вопрос партнёру 2);
- «Озеро» (CASCADE_x_LAKE) — свободное общение под медиатором.

Движение по каскаду:
  A: партнёр 1 ответил  -> партнёру 2 задаётся вопрос x_B
  B: партнёр 2 ответил  -> обоим приходит Озеро x_LAKE
  Озеро -> следующий каскад только когда ОБА нажали
  «🌊 Мы услышали друг друга. Дальше» (bot callback `next_cascade`
  и POST /api/next-cascade; флаги users.is_ready_for_next).

Здесь только тексты и описание движения; доставка (bot.send_message)
и вызовы database — в функциях этого же модуля: main.py и api.py
передают сюда объект бота (ленивый, см. api._get_bot).
"""

# ── Тексты ступеней ([проблема] заменяется на суть конфликта из онбординга) ──
CASCADE_1_A = (
    "Что вы чувствуете, когда ваш партнер [проблема]? "
    "Опишите свои ощущения, не обвиняя."
)
CASCADE_1_B = "Что вы чувствуете, когда слышите это от партнера?"
CASCADE_1_LAKE = (
    "Вы услышали чувства друг друга. Теперь у вас есть время просто поговорить. "
    "Не защищайтесь. Просто скажите, что вы услышали. "
    "Когда будете готовы идти дальше, нажмите кнопку ниже."
)
CASCADE_2_A = (
    "Чего вы на самом деле боитесь или в чем нуждаетесь в этой ситуации? "
    "(Например: 'Я боюсь остаться один')"
)
CASCADE_2_B = "А что чувствуете вы, когда партнер говорит о своей уязвимости?"
CASCADE_2_LAKE = (
    "Вы заговорили о самом глубоком. Здесь особенно легко занять оборону — "
    "и особенно ценно её не занимать. Поговорите о том, что вас тронуло. "
    "Правила те же: медиатор мягко переформулирует ваши слова. "
    "Когда почувствуете, что вас услышали, нажмите кнопку ниже."
)
CASCADE_3_INTEGRATION = (
    "Теперь, когда вы знаете истинные чувства друг друга, "
    "как вы можете решить вопрос с [проблема]?"
)

FIRST_STEP = "CASCADE_1_A"

# Ступень после ответа владельца ступени A/B; INTEGRATION — финал.
_AFTER_OWNER_ANSWER = {
    "CASCADE_1_A": "CASCADE_1_B",
    "CASCADE_1_B": "CASCADE_1_LAKE",
    "CASCADE_2_A": "CASCADE_2_B",
    "CASCADE_2_B": "CASCADE_2_LAKE",
}

# Ступень после Озера, когда оба нажали «Дальше»
_AFTER_LAKE = {
    "CASCADE_1_LAKE": "CASCADE_2_A",
    "CASCADE_2_LAKE": "CASCADE_3_INTEGRATION",
}

LAKE_BUTTON_TEXT = "🌊 Мы услышали друг друга. Дальше"
LAKE_CALLBACK = "next_cascade"


def next_after_owner_answer(step: str) -> str | None:
    """Ступень, в которую падаем после ответа владельца A/B-ступени."""
    return _AFTER_OWNER_ANSWER.get(step)


def next_after_lake(step: str) -> str | None:
    """Ступень за Озером (нужны оба «Дальше»); None — водопад кончился."""
    return _AFTER_LAKE.get(step)


def is_lake(step: str | None) -> bool:
    return bool(step) and step.endswith("_LAKE")


def is_finished(step: str | None) -> bool:
    return step == "CASCADE_3_INTEGRATION"


async def _topic(session_id: int) -> str:
    """«[проблема]» для подстановки — суть конфликта из онбординга."""
    import database  # flat-import стиль проекта

    info = await database.get_user_info(session_id)
    return (info or {}).get("about") or "ваша ситуация"


async def prompt_text_for(step: str, session_id: int) -> str:
    """Готовый текст подсказки: номер каскада + текст ступени EFT."""
    topic = await _topic(session_id)
    labels = {
        "CASCADE_1_A": ("🌊 Каскад 1 — Ступень A. Вопрос вам:", CASCADE_1_A.replace("[проблема]", topic)),
        "CASCADE_1_B": ("🌊 Каскад 1 — Ступень B. Вопрос вам:", CASCADE_1_B),
        "CASCADE_1_LAKE": ("🌊 Каскад 1 — Озеро", CASCADE_1_LAKE),
        "CASCADE_2_A": ("🌊 Каскад 2 — Ступень A. Вопрос вам:", CASCADE_2_A.replace("[проблема]", topic)),
        "CASCADE_2_B": ("🌊 Каскад 2 — Ступень B. Вопрос вам:", CASCADE_2_B),
        "CASCADE_2_LAKE": ("🌊 Каскад 2 — Озеро", CASCADE_2_LAKE),
        "CASCADE_3_INTEGRATION": (
            "🌊 Каскад 3 — Интеграция. Финальный вопрос обоим:",
            CASCADE_3_INTEGRATION.replace("[проблема]", topic),
        ),
    }
    label, body = labels.get(step, ("🌊 Сессия", "Продолжайте."))
    return f"{label}\n\n{body}"


async def enter_step(session_id: int, step: str, bot) -> None:
    """Входит в ступень: рассылает подсказку тем, кому она адресована.

    A — вопрос только партнёру 1 (создатель, id = session_id);
    B — только партнёру 2; LAKE — обоим, с Inline-кнопкой «Дальше»;
    INTEGRATION — финальный вопрос обоим.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    import database  # flat-import стиль проекта

    text = await prompt_text_for(step, session_id)
    partner = await database.get_session_partner(session_id, session_id)

    # best-effort: недоступность Telegram не должна ронять ход сессии
    if step.endswith("_A"):
        try:
            await bot.send_message(session_id, text)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR (therapy prompt A -> {session_id}): {e!r}")
        return

    if step.endswith("_B"):
        if partner is not None:
            try:
                await bot.send_message(partner, text)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR (therapy prompt B -> {partner}): {e!r}")
        return

    if step.endswith("_LAKE"):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(
                text=LAKE_BUTTON_TEXT, callback_data=LAKE_CALLBACK,
            )]]
        )
        for chat in (session_id, partner):
            if chat is None:
                continue
            try:
                await bot.send_message(chat, text, reply_markup=keyboard)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR (therapy lake -> {chat}): {e!r}")
        return

    if step == "CASCADE_3_INTEGRATION":
        for chat in (session_id, partner):
            if chat is None:
                continue
            try:
                await bot.send_message(chat, text)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR (therapy integration -> {chat}): {e!r}")


async def start_first_cascade(session_id: int, bot) -> None:
    """Запускает водопад: фиксирует Каскад 1, ступень A, и задаёт вопрос."""
    import database  # flat-import стиль проекта

    await database.set_current_step(session_id, FIRST_STEP)
    await enter_step(session_id, FIRST_STEP, bot)


async def ensure_started(session_id: int, bot) -> str | None:
    """Самолечение: запускает Каскад 1 для сессий «без водопада».

    Нужно для сессий, созданных ДО внедрения «Водопада» (у них нет строки
    в таблице sessions, а триггер «партнёр завершил онбординг» давно
    отработал и снова не сработает). Ступень запускается, только когда
    в сессии двое и у обоих заполнены профили (пол). Возвращает ступень.
    """
    import database  # flat-import стиль проекта

    step = await database.get_current_step(session_id)
    if step is not None:
        return step

    partner = await database.get_session_partner(session_id, session_id)
    if partner is None:
        return None
    owner_info = await database.get_user_info(session_id)
    partner_info = await database.get_user_info(partner)
    if not owner_info or not owner_info.get("gender"):
        return None
    if not partner_info or not partner_info.get("gender"):
        return None

    print(f"[Waterfall] сессия {session_id} без ступени, но оба прошли "
          "знакомство — запускаю Каскад 1")
    await start_first_cascade(session_id, bot)
    return FIRST_STEP


async def maybe_advance(session_id: int, sender_id: int, bot) -> str | None:
    """Движение по ступеням после записанного сообщения.

    Ступень A двигается ответом партнёра 1 (создателя), ступень B —
    ответом партнёра 2. Сообщения «не владельца» просто медиаторятся
    и ступень не сдвигают. Возвращает новую ступень или None.
    """
    import database  # flat-import стиль проекта

    step = await database.get_current_step(session_id)
    print(f"[Waterfall] Текущий шаг: {step} | отправитель: {sender_id}")
    if step is None or is_lake(step) or is_finished(step):
        return None
    if step not in _AFTER_OWNER_ANSWER:
        return None

    partner = await database.get_session_partner(session_id, session_id)
    owner = session_id if step.endswith("_A") else partner
    if sender_id != owner:
        return None

    new_step = next_after_owner_answer(step)
    if new_step is None:
        return None
    await database.set_current_step(session_id, new_step)
    await enter_step(session_id, new_step, bot)
    return new_step


async def try_advance_from_lake(session_id: int, bot) -> str | None:
    """Пробует пройти с Озера: если оба нажали «Дальше» — переходит дальше.

    Сбрасывает флаги is_ready_for_next. Возвращает новую ступень или None,
    если партнёр ещё не нажал.
    """
    import database  # flat-import стиль проекта

    step = await database.get_current_step(session_id)
    if not is_lake(step):
        return None
    ready, total = await database.get_cascade_readiness(session_id)
    if ready < 2 or total < 2:
        return None

    new_step = next_after_lake(step)
    if new_step is None:
        return None

    await database.set_ready_for_next(session_id, False)
    partner = await database.get_session_partner(session_id, session_id)
    if partner is not None:
        await database.set_ready_for_next(partner, False)
    await database.set_current_step(session_id, new_step)
    await enter_step(session_id, new_step, bot)
    return new_step
