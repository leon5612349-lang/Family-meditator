# Семейный медиатор

MVP проекта — Telegram Mini App, который помогает парам и семьям решать
конфликты с помощью нейросети-медиатора.

## Структура проекта

```
Family-meditator/
├── backend/          # Серверный код на Python (бот + API)
│   ├── main.py       # Точка входа Telegram-бота (aiogram)
│   └── requirements.txt
├── frontend/         # Telegram Mini App (пока пусто)
├── .env.example      # Шаблон секретных ключей
└── README.md
```

## Быстрый старт

### 1. Создайте виртуальное окружение

В корне проекта:

```bash
python -m venv venv
```

Активируйте его:

- **Windows (PowerShell):**
  ```powershell
  .\venv\Scripts\Activate.ps1
  ```
- **Windows (cmd):**
  ```cmd
  venv\Scripts\activate.bat
  ```
- **macOS / Linux:**
  ```bash
  source venv/bin/activate
  ```

### 2. Установите зависимости

```bash
pip install -r backend/requirements.txt
```

### 3. Настройте ключи

Скопируйте шаблон и заполните своими значениями:

```bash
cp .env.example .env
```

Необходимые ключи:

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather) в Telegram |
| `OPENAI_API_KEY` | Ключ OpenAI (пока не используется, нужен на следующих шагах) |

Файл `.env` не публикуется в репозитории — храните в нём только локальные копии ключей.

### 4. Запустите бота

```bash
python backend/main.py
```

Теперь в Telegram напишите боту `/start` — он должен ответить: **«Привет! Я медиатор»**.

## Планы развития

- [ ] Логика чата с нейросетью-медиатором
- [ ] FastAPI-сервер для общения с фронтендом
- [ ] Telegram Mini App (frontend/)
- [ ] База данных для истории сессий
