"""Единый слой DeepSeek для бота (main.py) и веб-API (api.py) — S1 аудита.

Раньше промпты, AsyncOpenAI-клиент и check_safety дублировались в двух
файлах и протекали рассинхронизацией (правки приходилось вносить дважды).
Теперь здесь единственные источники правды:

- build_rephrase_system_prompt() — системный промпт переформулировки
                     (роли сторон из онбординга, краткость+эмпатия)
- SUMMARY_PROMPT    — системный промпт резюме сессии (/finish)
- SAFETY_PROMPT     — системный промпт проверки безопасности
- SAFETY_RESPONSE   — жёсткий ответ при обнаружении угрозы
- client            — единственный AsyncOpenAI (один httpx-пул на процесс)
- check_safety()    — (is_dangerous, reason); fail-open при недоступности
- rephrase_text()   — переформулировка, бросает исключение при сбое API
- summarize_transcript() — резюме транскрипта, бросает исключение при сбое API

Аутентификация/логика доставки остаются в main.py и api.py.
"""

import json
import logging
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI

# Загружаем переменные окружения из файла .env (idempotent: и main, и api
# вызвают load_dotenv() сами — двойной вызов безвреден)
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# DeepSeek использует OpenAI-совместимый API
client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url="https://api.deepseek.com")

MODEL = "deepseek-chat"

# ПЕРЕформулировка собирается динамически: build_rephrase_system_prompt()
# (роли сторон из онбординга + правило краткости/эмпатии)


def build_rephrase_system_prompt(
    sender_name: str,
    sender_gender: str | None,
    partner_name: str,
    partner_gender: str | None,
    context: str = "",
) -> str:
    """Системный промпт переформулировки с ролями сторон (онбординг).

    Пол — 'М'/'Ж' или None (профиль не заполнен, например у старых
    пользователей или fallback-тестов из браузера): тогда роль описываем
    без пола, чтобы нейросеть не гадала и не путалась, кто есть кто.
    """
    def who(name: str, gender: str | None) -> str:
        return f"{name} (пол {gender})" if gender else name

    prompt = (
        f"Ты — медиатор. Сейчас {who(sender_name, sender_gender)} пишет "
        f"{who(partner_name, partner_gender)}. "
        f"Переформулируй сообщение {sender_name} в мягкое Я-сообщение. "
        f"Обращайся к партнеру по имени {partner_name}. "
        f"Учти контекст: если {sender_name} пишет 'ты меня не слушаешь', "
        f"это значит, что {sender_name} чувствует себя неуслышанным "
        f"партнером {partner_name}."
    )
    if context:
        prompt += f" Контекст от {sender_name}: {context}"
    # Баланс краткости и теплоты: объём ограничиваем предложениями,
    # а не доле от длины сообщения — эмпатия не должна страдать
    prompt += (
        " Твой ответ должен быть кратким (максимум 2-3 предложения), но "
        "обязательно начинай с признания чувств партнера "
        "(например: 'Я слышу, что тебе сейчас обидно...'). "
        "Не пиши сухо. Сохраняй баланс между краткостью и теплотой."
    )
    return prompt


def dynamic_max_tokens(text: str) -> int:
    """Бюджет токенов ответа под длину сообщения пользователя.

    Правило промпта — «максимум 2-3 предложения» с обязательной эмпатией
    в начале: на русский это ~в среднем 60-100 токенов, поэтому коротким
    сообщениям даём 100 (иначе третье предложение обрубается), а
    развёрнутым — спокойные 200. Меньше всё равно нельзя без обрубков.
    """
    words = len(text.split())
    return 100 if words < 10 else 200

# Анализ всей сессии по команде /finish
SUMMARY_PROMPT = (
    "Ты — семейный психолог-медиатор. Перед тобой диалог двух партнёров, "
    "прошедший через медиацию. Проанализируй конфликт, найди компромисс "
    "и напиши краткое резюме: в чём была суть разногласий и как партнёрам "
    "договориться дальше."
)

# ── Проверка безопасности сообщения ─────────────────────────────
# Контекстная проверка через нейросеть, а не поиск слов: она не даёт
# ложных срабатываний на "убить время" и опечатках ("избавить"→"избить").
SAFETY_PROMPT = (
    "Ты — эксперт по безопасности. Проанализируй текст пользователя. "
    "Твоя задача — выявить только РЕАЛЬНУЮ угрозу: прямое насилие, угрозу "
    "жизни, суицидальные намерения или призывы к самоповреждению. "
    "Игнорируй гиперболы, ругательства, бытовые фразы (например, 'убить "
    "время') и опечатки. Отвечай строго в формате JSON: "
    '{"is_dangerous": true/false, "reason": "краткое пояснение"}. '
    "Поле reason должно быть кратким, не более 50 символов."
)

# Жёстко заданный ответ пользователю при обнаружении реальной угрозы
# (одинаковый в боте и в Mini App — берите из этой же константы)
SAFETY_RESPONSE = (
    "Я слышу, что ситуация очень серьезная и небезопасная. Я не могу помочь "
    "в вопросах, связанных с угрозами. Пожалуйста, остановитесь. Если есть "
    "угроза жизни, позвоните по номеру 112. Если вам нужна психологическая "
    "помощь, обратитесь к специалисту."
)


async def check_safety(text: str) -> tuple[bool, str]:
    """Контекстная проверка сообщения на РЕАЛЬНУЮ угрозу через DeepSeek.

    Возвращает (is_dangerous, reason). Для скорости (модель deepseek-chat
    единственная в API, ускоряем сам запрос):
    - temperature=0 — однозначный вердикт, без креатива;
    - max_tokens=100 — малюсенький JSON перегоняется быстро;
    - response_format json_object — DeepSeek отдаёт строгий JSON (слово
      "json" в промпте есть, как требует документация).

    Страховочная политика: если проверка недоступна (сеть, лимиты),
    считаем текст БЕЗОПАСНЫМ — из-за сбоя сервиса сообщения
    пользователей не должны переставать доставляться.
    """
    if not text.strip():
        return False, ""

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SAFETY_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_tokens=100,
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "").strip()
        # На всякий случай срезаем markdown-обёртку вида ```json ... ```
        if raw.startswith("```"):
            raw = raw.strip("`").removeprefix("json").strip()
    except Exception as e:  # noqa: BLE001 — fail-safe: доступ важнее блокировки
        logging.getLogger("app").warning(
            "check_safety недоступна (%r) — сообщение пропущено как безопасное", e
        )
        return False, "проверка недоступна"

    try:
        result = json.loads(raw)
        return bool(result.get("is_dangerous")), str(result.get("reason", ""))
    except Exception:  # noqa: BLE001 — H4: усечённый/кривой JSON не оставим без следа
        print(f"ОШИБКА ПАРСИНГА SAFETY: {raw}")
        return False, "проверка недоступна"


async def rephrase_text(
    text: str,
    sender_name: str,
    sender_gender: str | None = None,
    partner_name: str = "партнер",
    partner_gender: str | None = None,
    context: str = "",
) -> str:
    """Переформулировка текста пользователя в мягкое 'Я-сообщение'.

    Онбординг: принимает роли сторон (имя+пол отправителя и получателя)
    и опциональный контекст (суть конфликта от отправителя) — чтобы
    нейросеть не путала, кто есть кто, и понимал ситуацию.

    При сбое API исключение БРОСАЕТСЯ наружу — обработка остаётся в месте
    вызова (бот отвечает пользователю, API отдаёт 500): слияние
    ошибки с дефолтной переформулировкой скрыло бы факт сбоя.
    """
    system = build_rephrase_system_prompt(
        sender_name, sender_gender, partner_name, partner_gender, context
    )
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        # Анти-раздувание: динамический бюджет + штраф за повторы мыслей
        max_tokens=dynamic_max_tokens(text),
        frequency_penalty=0.3,
    )
    return response.choices[0].message.content


async def summarize_transcript(transcript: str) -> str:
    """Резюме сессии по транскрипту (пары оригинал/переформулировка).

    Как и rephrase_text, при сбое API прокидывает исключение —
    вариант обработки в вызывающем коде (у бота и API они разные).
    """
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": transcript},
        ],
    )
    return response.choices[0].message.content


# «Раскрыть подробнее»: помощь сформулировать развёрнутую просьбу.
# Ключевые отличия от переформулировки: второй абзац-длиннее, от первого
# лица, с конкретной просьбой — то есть пользователь получает заготовку,
# которую можно отправить партнёру почти как есть.
EXPAND_PROMPT = (
    "Ты — семейный психолог-медиатор. Пользователь поделился тем, что его "
    "задело в партнёре. Помоги ему сформулировать, что именно он хочет "
    "сказать партнёру: назови чувство, стоящую за ним потребность и "
    "конкретную просьбу или договорённость. Пиши от первого лица "
    "('я чувствую…', 'мне важно…', 'давай попробуем…'), тепло и "
    "по-человечески, 3-5 предложений, без осуждения партнёра и без метафор. "
    "Не задавай вопросов — дай готовый текст-заготовку, который можно "
    "отправить партнёру почти без правок."
)


async def expand_text(original_text: str, rephrased_text: str = "", context: str = "") -> str:
    """Развёрнутый совет «что именно сказать партнёру» (кнопка-раскрытие).

    Берём последнее сообщение пользователя: оригинал — главный материал,
    мягкая переформулировка — заготовка первого предложения. Как и
    rephrase_text, при сбое API прокидывает исключение наружу.
    """
    user_content = (
        f"Сообщение пользователя: {original_text}\n"
        f"Уже сделанная мягкая переформулировка: {rephrased_text or '(нет)'}"
    )
    if context:
        user_content += f"\nКонтекст от пользователя: {context}"
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": EXPAND_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_tokens=250,
        frequency_penalty=0.2,
    )
    return response.choices[0].message.content
