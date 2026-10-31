"""Единый слой DeepSeek для бота (main.py) и веб-API (api.py) — S1 аудита.

Раньше промпты, AsyncOpenAI-клиент и check_safety дублировались в двух
файлах и протекали рассинхронизацией (правки приходилось вносить дважды).
Теперь здесь единственные источники правды:

- SUMMARY_PROMPT    — системный промпт резюме сессии (/finish)
- SAFETY_PROMPT     — системный промпт проверки безопасности
- SAFETY_RESPONSE   — жёсткий ответ при обнаружении угрозы
- _get_client()     — единственный AsyncOpenAI к ZvenoAI (ленивый —
                     с ключом ZVENO_API_KEY; «один httpx-пул на процесс»)
- check_safety()    — (is_dangerous, reason); fail-open при недоступности
- summarize_transcript() — резюме транскрипта, бросает исключение при сбое API
- analyze_partner_message() — «Расшифровка»: разбор сообщения партнёра
                     по плану ЭФТ (бросает исключение при сбое API)
- expand_text()     — развёрнутый совет «что сказать партнёру»
- transcribe_voice() — распознавание голосовых через ZvenoAI, бросает
                     исключение при сбое API или отсутствии ключа

Модель диалога: живая переписка БЕЗ переформулирования (функции
rephrase_text/build_rephrase_system_prompt/dynamic_max_tokens удалены
после отключения автоформулировки сообщений). ВСЕ запросы идут через
ZvenoAI (base https://api.zveno.ai/v1) с ключом ZVENO_API_KEY:
- текст — deepseek/deepseek-v4.1-flash;
- аудио — openai/gpt-4o-mini-transcribe.
Аутентификация/логика доставки остаются в main.py и api.py.
"""

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI, BadRequestError

# Загружаем переменные окружения из файла .env (idempotent: и main, и api
# вызвают load_dotenv() сами — двойной вызов безвреден)
load_dotenv()

# ZvenoAI — единый OpenAI-совместимый шлюз для текста и аудио.
ZVENO_BASE_URL = "https://api.zveno.ai/v1"
# Текстовая модель (безопасность, анализ, суммаризация, советы)
TEXT_MODEL = "deepseek/deepseek-v4.1-flash"

_client = None


def _get_client():
    """Ленивый единственный AsyncOpenAI-клиент ZvenoAI.

    None — если ZVENO_API_KEY не задан в .env (функции сами решают,
    как упасть: check_safety пропускает сообщение, разборы бросают).
    """
    global _client
    key = os.getenv("ZVENO_API_KEY")
    if not key:
        return None
    if _client is None:
        _client = AsyncOpenAI(api_key=key, base_url=ZVENO_BASE_URL)
    return _client


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
    """Контекстная проверка сообщения на РЕАЛЬНУЮ угрозу (DeepSeek через Zveno).

    Возвращает (is_dangerous, reason). Для скорости:
    - temperature=0 — однозначный вердикт, без креатива;
    - max_tokens=100 — малюсенький JSON перегоняется быстро;
    - response_format json_object — ответ строго в JSON (слово
      "json" в промпте есть, как требует документация).

    Страховочная политика: если проверка недоступна (сеть, лимиты,
    ключ ZVENO_API_KEY не задан), считаем текст БЕЗОПАСНЫМ — из-за сбоя
    сервиса сообщения пользователей не должны переставать доставляться.
    """
    if not text.strip():
        return False, ""

    try:
        zveno = _get_client()
        if zveno is None:
            raise RuntimeError("ZVENO_API_KEY не задан — текстового провайдера нет")
        response = await zveno.chat.completions.create(
            model=TEXT_MODEL,
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


async def summarize_transcript(transcript: str) -> str:
    """Резюме сессии по транскрипту (сырые сообщения участников).

    При сбое API исключение прокидывается наружу — вариант обработки
    в вызывающем коде (у бота и API они разные).
    """
    zveno = _get_client()
    if zveno is None:
        raise RuntimeError("ZVENO_API_KEY не задан — текстового провайдера нет")
    response = await zveno.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": transcript},
        ],
    )
    return response.choices[0].message.content


# ── Расшифровка: глубокий разбор сообщения партнёра в стиле ЭФТ ──
# Задача — не переформулировать, а РАСКРЫТЬ структуру защитного
# сообщения: факты → настоящие эмоции под маской → уязвимую потребность →
# здоровый вариант фразы. Ответ строго структурирован (нумерованный
# план), строки переноса сохраняются в чате.
ANALYZE_PROMPT = (
    "Ты — семейный психолог-медиатор, работающий в подходе ЭФТ "
    "(эмоционально-фокусированная терапия, Сью Джонсон) и теории "
    "привязанности. Твоя задача — взять агрессивное или защитное "
    "сообщение от одного партнера и расшифровать его для другого. "
    "Структурируй ответ строго по плану:\n"
    "1. Факты: Что объективно произошло (без оценок).\n"
    "2. Эмоции и маски: Что партнер почувствовал на самом деле и как "
    "он это выразил (агрессия, обида, уход в себя).\n"
    "3. Истинная потребность (уязвимость): Чего партнер на самом деле "
    "боится или в чем нуждается (например: 'Я боюсь остаться один', "
    "'Мне нужно внимание').\n"
    "4. Как это сказать здоровым языком (Я-сообщение): Предложи 1-2 "
    "варианта того, как партнер мог бы сказать это без обвинений.\n\n"
    "Правила: пиши бережно и по-человечески, без диагнозов и ярлыков; "
    "не упрекай ни отправителя, ни получателя; каждому пункту — свой "
    "короткий абзац (2-4 предложения); сохраняй нумерацию плана. "
    "Итог должен читаться как профессиональный разбор, а не как простое "
    "перефразирование."
)


async def analyze_partner_message(text: str) -> str:
    """Глубокий разбор («Расшифровка») защитного сообщения партнёра.

    Возвращает структурированный разбор по плану ЭФТ. При сбое API
    исключение БРОСАЕТСЯ наружу — обработка на месте вызова (бот: block
    «Нейросеть недоступна»; API: 500).
    """
    zveno = _get_client()
    if zveno is None:
        raise RuntimeError("ZVENO_API_KEY не задан — текстового провайдера нет")
    response = await zveno.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": ANALYZE_PROMPT},
            {"role": "user", "content": text},
        ],
        # Разбор длиннее переформулировки: 4 пункта по 2-4 предложения
        max_tokens=350,
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

    Берём последнее сообщение пользователя: оригинал — главный материал.
    При сбое API исключение прокидывается наружу.
    """
    zveno = _get_client()
    if zveno is None:
        raise RuntimeError("ZVENO_API_KEY не задан — текстового провайдера нет")
    user_content = (
        f"Сообщение пользователя: {original_text}\n"
        f"Уже сделанная мягкая переформулировка: {rephrased_text or '(нет)'}"
    )
    if context:
        user_content += f"\nКонтекст от пользователя: {context}"
    response = await zveno.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": EXPAND_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_tokens=250,
        frequency_penalty=0.2,
    )
    return response.choices[0].message.content


# ── Распознавание голосовых сообщений (ZvenoAI) ──────────────────────────
# Тот же шлюз и тот же ключ, что и для текста, но аудио-модель.
# (клиент общий — см. _get_client() наверху)
#
# Браузеры (MediaRecorder) пишут WebM/Opus — не все транскрипционные
# модели принимают этот контейнер: Zveno может вернуть
# «Audio file might be corrupted or unsupported». Поэтому два прохода:
#   1) прямая передача .webm с правильным (имя, MIME);
#   2) при отказе формата — локальная конвертация в OGG через pydub
#      (нужен ffmpeg в PATH) и повторная отправка.
ZVENO_MODEL = "openai/gpt-4o-mini-transcribe"


def _convert_audio_to_ogg(src: str) -> str | None:
    """Конвертирует запись в OGG (pydub + ffmpeg). None — недоступно.

    pydub сам по себе не умеет WebM: требуется ffmpeg в PATH
    (Windows: скачать сборку ffmpeg и добавить в PATH).
    """
    try:
        from pydub import AudioSegment  # лениво: pydub — необязательный пакет
    except ImportError:
        print("ВНИМАНИЕ: pydub не установлен — конвертация аудио недоступна "
              "(pip install pydub, и ffmpeg в PATH)")
        return None
    dst = src.rsplit(".", 1)[0] + ".ogg"
    try:
        audio = AudioSegment.from_file(src)
        audio.export(dst, format="ogg")
        return dst
    except Exception as e:  # noqa: BLE001 — чаще всего нет ffmpeg
        print(f"ERROR (pydub конвертация {dst!r}): {e!r} — проверьте, что ffmpeg в PATH")
        return None


def _is_format_reject(e: BaseException) -> bool:
    """Определяем отказ по ФОРМАТУ (конвертация не поможет при других ошибках)."""
    s = str(e).lower()
    return "corrupt" in s or "unsupport" in s or "format" in s or "audio file" in s


async def transcribe_voice(file_path: str) -> str:
    """Распознаёт голосовое сообщение (webm/ogg) через ZvenoAI.

    Первый проход отправляет файл как есть с корректным именем и
    MIME-типом (кортеж (filename, fileobj, content_type) — библиотека
    openai формирует multipart ровно с этими данными). Если Zveno
    отклоняет контейнер WebM («corrupted or unsupported») — конвертируем
    в OGG через pydub/ffmpeg и повторяем. При сбоях бросает исключения
    (RuntimeError — проблема конфигурации/конвертации, прочие — API).
    """
    zveno = _get_client()
    if zveno is None:
        raise RuntimeError("ZVENO_API_KEY не задан — распознавание речи не настроено")

    async def _send(path: str, filename: str, mime: str) -> str:
        with open(path, "rb") as audio:
            result = await zveno.audio.transcriptions.create(
                model=ZVENO_MODEL,
                file=(filename, audio, mime),
            )
        return (result.text or "").strip()

    base = os.path.basename(file_path) or "voice.webm"
    try:
        return await _send(file_path, base if base.endswith(".webm") else "voice.webm", "audio/webm")
    except BadRequestError as e:
        if not _is_format_reject(e):
            raise
        print(f"[Zveno] аудио отклонено по формату ({e}) — конвертирую в OGG")

    ogg = await asyncio.to_thread(_convert_audio_to_ogg, file_path)
    if ogg is None:
        raise RuntimeError(
            "ZvenoAI не принимает запись WebM, а локальная конвертация "
            "недоступна: установите pydub (pip install pydub) и ffmpeg"
        )
    try:
        return await _send(ogg, os.path.basename(ogg), "audio/ogg")
    finally:
        try:
            os.remove(ogg)
        except OSError:  # noqa: BLE001
            pass
