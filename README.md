# 🤫 Whisper Bot — эфемерные сообщения в Telegram

Inline-бот: сообщение видит только тот, кому оно адресовано. Бота **не нужно
добавлять в чат** — он работает через inline-режим, то есть в любом чате,
группе или канале, где инлайн-боты разрешены.

```
@secretnobot Привет @verifure
```

Сверху всплывает результат **«Отправить шёпот для @verifure»** → в чат уходит
карточка с кнопкой **«Прочитать 🔓»** → текст видит только @verifure во
всплывающем окне. Остальные получают «🤐 Это не тебе».

Весь бот — **один файл `main.py`**, без пакетов и папок. Это намеренно: такую
структуру невозможно случайно сломать при загрузке на GitHub.

---

## Как это устроено

| Что | Метод / объект Bot API | Зачем |
|---|---|---|
| Приём запроса из строки ввода | `inline_query` update | Ловим текст после `@имя_бота` |
| Ответ вариантами | `answerInlineQuery` + `InlineQueryResultArticle` | `cache_time=0`, `is_personal=True` — чтобы Telegram не показал чужой результат |
| Текст, который уйдёт в чат | `InputTextMessageContent` | В чат уходит только «Шёпот для @user», без содержимого |
| Кнопка под сообщением | `InlineKeyboardMarkup` + `callback_data` | В `callback_data` лежит только 16-символьный id, текст остаётся на сервере |
| Чтение шёпота | `callback_query` + `answerCallbackQuery(show_alert=True)` | Алерт видит только нажавший — это и есть эфемерность |
| Сжигание после прочтения | `editMessageText(inline_message_id=…)` | Правим сообщение прямо в чужом чате |

Получатель определяется по `callback_query.from_user.username`, потому что Bot
API не умеет резолвить @username в user_id — значит, у адресата должен быть
публичный username. Автор всегда может перечитать своё сообщение.

Всплывающее окно Telegram вмещает **до 200 символов** — бот проверяет длину
заранее и не даёт отправить больше.

---

## Структура репозитория

```
secret-whisper-bot/
├── main.py           # весь бот: конфиг, хранилище, хендлеры, запуск
├── requirements.txt
├── Procfile
├── railway.json
├── runtime.txt
├── Dockerfile
├── .env.example
└── .gitignore
```

На GitHub все эти файлы должны лежать **прямо в корне репозитория**, без
вложенных папок.

---

## 1. Настройка бота в @BotFather

1. `/newbot` → получите **BOT_TOKEN**.
2. **Обязательно** `/setinline` → выберите бота → введите подсказку, например
   `текст @username`. Без этого шага inline-режим работать не будет.
3. Желательно `/setinlinefeedback` → `Disabled`.
4. Опционально `/setcommands`:
   ```
   start - Как пользоваться ботом
   help - Как пользоваться ботом
   ```

---

## 2. Локальный запуск

```bash
git clone <ваш-репозиторий> && cd secret-whisper-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # впишите BOT_TOKEN
export $(grep -v '^#' .env | xargs)
python main.py
```

---

## 3. Деплой на Railway

1. Залейте код на GitHub — **файлы должны лежать в корне репозитория**, не в
   подпапке.
2. На [railway.com](https://railway.com) → **New Project → Deploy from GitHub repo**.
3. Variables → добавьте:
   ```
   BOT_TOKEN = 123456789:AA...
   ```
4. В логе деплоя должно появиться `Бот @имя запущен`.

Проверить структуру перед пушем:

```bash
git ls-files | sort
```

В выводе должны быть строки `main.py`, `requirements.txt`, `Procfile` —
**без** вложенных папок.

По умолчанию используется long polling — публичный домен и открытый порт не
нужны.

### Чтобы тексты переживали рестарты (рекомендуется)

`+ New → Database → Add Redis` в том же проекте, затем в сервисе бота
добавьте переменную:

```
REDIS_URL = ${{Redis.REDIS_URL}}
```

Без Redis шёпоты хранятся в памяти процесса и пропадут при каждом редеплое.

### Режим webhook (по желанию)

1. Settings → Networking → **Generate Domain**.
2. Переменные:
   ```
   USE_WEBHOOK = true
   PUBLIC_URL = https://<ваш-домен>.up.railway.app
   ```

---

## 4. Переменные окружения

| Переменная | По умолчанию | Описание |
|---|---|---|
| `BOT_TOKEN` | — | **Обязательно.** Токен от @BotFather |
| `REDIS_URL` | — | Redis для постоянного хранения |
| `WHISPER_TTL` | `604800` | Сколько секунд живёт шёпот (7 дней) |
| `BURN_AFTER_READING` | `false` | Сжигать после первого прочтения получателем |
| `NOTIFY_SENDER` | `true` | Писать автору в ЛС о прочтении |
| `MAX_TEXT_LEN` | `200` | Лимит длины текста |
| `USE_WEBHOOK` | `false` | Webhook вместо polling |
| `PUBLIC_URL` | авто из `RAILWAY_PUBLIC_DOMAIN` | Базовый URL для webhook |
| `WEBHOOK_SECRET` | часть токена | `secret_token` для проверки запросов Telegram |

---

## 5. Приватность

* Текст шёпота никогда не попадает в сообщение чата и в `callback_data`.
* В логи содержимое не пишется.
* Записи автоматически удаляются по TTL, а при `BURN_AFTER_READING=true` —
  сразу после прочтения.
* Это не end-to-end шифрование: у владельца сервера технически есть доступ к
  хранилищу.

## Лицензия

MIT
