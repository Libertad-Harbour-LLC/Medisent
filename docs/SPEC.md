# ТЗ: Telegram-бот подбора поставщиков медизделий

Задание для Claude Code. Собирается инкрементально: каждый этап заканчивается работающим и проверяемым куском. Не переходи к следующему, пока текущий не проходит критерий приёмки.

---

## 0. Что строим

Telegram-бот, который по запросу (текст, фото, голос, файл) находит поставщиков изделия, проверяет регистрацию изделия в реестрах Росздравнадзора, собирает отчёт, спрашивает у владельца, кого выбрать, пишет выбранному письмо, ловит ответ и собирает по нему коммерческое предложение в PDF с печатью и подписью.

Владелец бота один — авторизация по whitelist Telegram ID, без многопользовательского режима.

### Стек

| Слой | Решение | Почему |
|---|---|---|
| Бот | Python 3.12 + aiogram 3 | kp-builder уже на Python, все SDK нативные |
| БД | PostgreSQL 16 | уже развёрнут |
| Веб-доступ к данным | Mathesar | таблицы Mathesar = реальные таблицы Postgres, отдельного хранилища не создаёт |
| Мультимодальность | Gemini API | фото + аудио + текст одной моделью |
| Поиск | Perplexity API | ответ с источниками |
| Скрейпинг | Firecrawl | сайты поставщиков |
| Почта | Gmail API | отправка и приём |
| PDF | скилл kp-builder | приложен отдельным файлом |

**Без n8n и без CRM.** Оркестрация — код бота.

### Ссылки на документацию

- Telegram Bot API — https://core.telegram.org/bots/api
- aiogram — https://docs.aiogram.dev
- Gemini API — https://ai.google.dev/gemini-api/docs
- Perplexity API — https://docs.perplexity.ai
- Firecrawl — https://docs.firecrawl.dev
- Gmail API — https://developers.google.com/gmail/api
- Mathesar — https://github.com/mathesar-foundation/mathesar

Версии API могли измениться — сверься с документацией перед написанием клиента, не полагайся на память.

### Реестры Росздравнадзора (критично, читай внимательно)

Реестров **два**, и проверять надо оба:

1. **До 01.03.2025** — https://roszdravnadzor.gov.ru/services/misearch
   Государственный реестр медицинских изделий. Здесь основная масса записей.
2. **После 01.03.2025** — https://elk.roszdravnadzor.gov.ru/widget/
   Отдельный реестр, появился из-за новых Правил регистрации (постановление Правительства РФ от 30.11.2024 № 1684). Записи по заявлениям, поданным с 1 марта 2025.

Дополнительно: https://roszdravnadzor.gov.ru/services/unrega — информационные письма об изъятиях и претензиях к качеству. Негативный сигнал для базы критериев.

**Про elk:** виджет — это React-приложение (create-react-app), обычный скрейп вернёт пустую оболочку. За ним есть публичный gateway вида `https://elk.roszdravnadzor.gov.ru/public-gateway/med-product/api/v1/...` — открой виджет в браузере, сними реальные запросы из DevTools → Network и работай с JSON напрямую. Firecrawl для elk не применяй.

**Что реестр НЕ говорит:** есть ли изделие у конкретного поставщика. Дилеров в реестре нет вообще. Реестр отвечает только на вопросы «зарегистрировано ли изделие», «кто держатель РУ», «действует ли удостоверение». В отчёте это два разных поля, сливать в одно «проверено в Росздравнадзоре» запрещено.

---

## Этап 0. Окружение и деплой

### Где это живёт

**VPS с Docker Compose** (Ubuntu 22.04+, 4 ГБ RAM минимум) либо постоянно включённая машина владельца. Четыре контейнера: `bot`, `postgres`, `mathesar`, `chromium` (для рендера PDF, если выносить отдельно).

**Serverless (Vercel и аналоги) не подходит — не пытайся туда деплоить.** Причины конкретные:
- Опрос Gmail нужен раз в 3–5 минут. На Hobby-плане Vercel Cron ограничен одним запуском в сутки — выражения чаще суточного просто не проходят деплой. Минутная частота только на Pro.
- Максимальная длительность функции — 300 с на Hobby и Pro (800 с на Pro с fluid compute). Обход 5–10 сайтов поставщиков через Firecrawl плюс два реестра в это укладывается не всегда.
- Playwright с Chromium для сборки PDF — тяжёлый бандл и холодный старт на каждый вызов.
- Бот качает файлы из Telegram и держит состояние сессии; постоянный процесс проще и дешевле, чем городить это на функциях.

Если владелец всё же хочет Vercel — туда можно вынести только приём вебхука Telegram, который кладёт апдейт в очередь. Вся обработка остаётся на VPS. Скажи об этом прямо, не собирай гибрид молча.

### Файлы окружения

`docker-compose.yml` с четырьмя сервисами, healthcheck на postgres, `restart: unless-stopped` на боте. Volume для данных Postgres и для `skills/kp-builder/assets/`.

`.env.example` — все переменные с пустыми значениями:

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_ID=            # единственный разрешённый пользователь
DATABASE_URL=postgresql://...
GEMINI_API_KEY=
PERPLEXITY_API_KEY=
FIRECRAWL_API_KEY=
LLM_REPORT_MODEL=             # модель для отчёта
LLM_EMAIL_MODEL=              # модель для письма
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=
GMAIL_SENDER=                 # с какого адреса шлём
FORWARD_TO_EMAIL=             # куда пересылать приложенные файлы
LOG_LEVEL=INFO
DAILY_API_BUDGET_USD=         # мягкий потолок, при превышении бот предупреждает
```

**Gmail OAuth scopes:** `gmail.send` для отправки и `gmail.readonly` для чтения ответов. Если понадобятся метки — `gmail.modify` вместо readonly. Не запрашивай полный `mail.google.com`.

### Язык и заглушки

Бот общается по-русски. Все сообщения пользователю — в отдельном модуле `bot/texts.py`, не разбросаны по хендлерам.

Системные инструкции `prompts/report.md` и `prompts/email.md` владелец даст позже. **Создай рабочие заглушки**, чтобы этапы 5 и 7 можно было довести до конца и проверить: минимальный промпт, который выдаёт корректный JSON нужной структуры. В шапке каждого файла — комментарий, что это временная версия под замену.

**Приёмка этапа 0:** `docker compose up` поднимает все контейнеры, бот отвечает владельцу, отсутствие ключей Perplexity и Firecrawl не мешает запуску — только логируется предупреждение.

---

## Этап 1. Каркас и база

### Структура проекта

```
supplier-bot/
├── bot/
│   ├── main.py              # точка входа, роутеры aiogram
│   ├── handlers/
│   │   ├── intake.py        # приём текста/фото/голоса/файлов
│   │   ├── selection.py     # обработка голосового выбора
│   │   └── admin.py         # /stats, /blacklist, /session
│   ├── services/
│   │   ├── gemini.py
│   │   ├── perplexity.py
│   │   ├── firecrawl.py
│   │   ├── registry.py      # оба реестра Росздравнадзора
│   │   ├── report.py        # сборка отчёта
│   │   ├── criteria.py      # извлечение критериев
│   │   ├── mail.py          # Gmail: отправка и приём
│   │   └── kp.py            # обёртка над kp-builder
│   ├── db/
│   │   ├── models.py
│   │   ├── repo.py          # весь SQL здесь, больше нигде
│   │   └── migrations/
│   └── config.py            # всё из .env, ключей в коде нет
├── prompts/
│   ├── report.md            # системная инструкция отчёта (владелец задаст позже)
│   ├── email.md             # системная инструкция письма (владелец задаст позже)
│   └── criteria.md          # извлечение критериев из обоснования
├── skills/kp-builder/       # распаковать приложенный .skill
├── tests/
├── .env.example
└── README.md
```

### Схема БД

```sql
-- сессия подбора: без неё непонятно, к чему относится «беру второго»
CREATE TABLE requests (
  id            BIGSERIAL PRIMARY KEY,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  raw_input     TEXT,                    -- что прислал владелец
  input_kind    TEXT,                    -- text | photo | voice | file
  product       TEXT NOT NULL,           -- распознанное изделие
  status        TEXT NOT NULL,           -- search|report|awaiting_choice|mail_sent|awaiting_reply|kp|closed
  token         TEXT UNIQUE NOT NULL     -- RFQ-2026-041, идёт в тему письма
);

CREATE TABLE suppliers (
  id            BIGSERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  domain        TEXT,
  tax_id        TEXT,                    -- ИНН
  country       TEXT,
  email         TEXT,
  phone         TEXT,
  first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
  found_via     TEXT                     -- поисковый запрос, который его привёл
);
CREATE UNIQUE INDEX suppliers_domain_uq ON suppliers (lower(domain)) WHERE domain IS NOT NULL;
CREATE UNIQUE INDEX suppliers_tax_uq    ON suppliers (tax_id)        WHERE tax_id IS NOT NULL;

-- результат проверки по конкретной заявке
CREATE TABLE candidates (
  id              BIGSERIAL PRIMARY KEY,
  request_id      BIGINT REFERENCES requests(id),
  supplier_id     BIGINT REFERENCES suppliers(id),
  site_claims     BOOLEAN,        -- поставщик заявляет наличие на своём сайте
  site_url        TEXT,
  site_price      NUMERIC,
  ru_number       TEXT,           -- номер РУ из реестра
  ru_holder       TEXT,           -- держатель РУ
  ru_valid        BOOLEAN,
  ru_registry     TEXT,           -- misearch | elk
  ru_checked_at   TIMESTAMPTZ,
  unrega_flags    JSONB,          -- информационные письма
  raw             JSONB           -- сырые ответы всех источников
);

CREATE TABLE quote_requests (
  id            BIGSERIAL PRIMARY KEY,
  request_id    BIGINT REFERENCES requests(id),
  supplier_id   BIGINT REFERENCES suppliers(id),
  sent_at       TIMESTAMPTZ,
  gmail_thread  TEXT,
  message_id    TEXT,             -- RFC Message-ID отправленного
  replied_at    TIMESTAMPTZ,
  reply_text    TEXT,
  price         NUMERIC,
  currency      TEXT,
  lead_time     TEXT,
  status        TEXT              -- sent | replied | silent | refused
);

CREATE TABLE orders (
  id            BIGSERIAL PRIMARY KEY,
  supplier_id   BIGINT REFERENCES suppliers(id),
  ordered_at    DATE,
  items         JSONB,
  amount        NUMERIC,
  currency      TEXT,
  promised_date DATE,
  actual_date   DATE,
  rating        SMALLINT CHECK (rating BETWEEN 1 AND 5)
);

-- отдельной таблицей, не флажком: нужны причина, дата и история
CREATE TABLE blacklist (
  id            BIGSERIAL PRIMARY KEY,
  supplier_id   BIGINT REFERENCES suppliers(id),
  added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  reason        TEXT NOT NULL,
  order_id      BIGINT REFERENCES orders(id),
  lifted_at     TIMESTAMPTZ
);

-- самопополняемая база критериев
CREATE TABLE criteria (
  id            BIGSERIAL PRIMARY KEY,
  text          TEXT NOT NULL,      -- «срок поставки важнее цены при разнице до 10%»
  direction     TEXT,               -- plus | minus
  weight        REAL DEFAULT 1.0,
  times_seen    INT DEFAULT 1,
  first_seen    TIMESTAMPTZ DEFAULT now(),
  last_seen     TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE criteria_events (
  id            BIGSERIAL PRIMARY KEY,
  criterion_id  BIGINT REFERENCES criteria(id),
  request_id    BIGINT REFERENCES requests(id),
  supplier_id   BIGINT REFERENCES suppliers(id),
  transcript    TEXT,               -- сырое голосовое, источник критерия
  created_at    TIMESTAMPTZ DEFAULT now()
);
```

Дедупликация поставщиков — целиком на уникальных индексах: повторная находка делает `INSERT ... ON CONFLICT DO UPDATE`, а не вторую строку. Матчинг по названию компании не делай, названия пишут по-разному.

Подними Mathesar через Docker и подключи к этой же базе — он даст веб-таблицу для ручной правки без отдельного хранилища.

**Приёмка этапа 1:** миграции применяются, бот отвечает на `/start` только владельцу, Mathesar показывает все таблицы.

---

## Этап 2. Проверка в реестрах

Делай этот этап вторым, до поиска и до отчёта. Именно он отличает систему от обычного запроса в Perplexity, и именно он может не заработать — лучше узнать это сейчас.

`services/registry.py`, одна функция:

```python
def check_product(name: str, ru_number: str | None = None) -> RegistryResult
```

Ищет по обоим реестрам, возвращает: найдено или нет, номер РУ, держатель, статус действия, из какого реестра, ссылку на карточку, срез сырого JSON.

Порядок работы:
1. Открой https://elk.roszdravnadzor.gov.ru/widget/ в браузере, найди в DevTools запросы к `/public-gateway/med-product/api/v1/...`, воспроизведи их HTTP-клиентом.
2. Для https://roszdravnadzor.gov.ru/services/misearch разбери формат GET-параметров поиска (пример из выдачи: `?q_mi_label_application=ФСР+2010/08183`) и парси результат.
3. Отдельной функцией дёргай https://roszdravnadzor.gov.ru/services/unrega по названию и держателю РУ.

Требования:
- Никакого LLM в этом модуле. Только детерминированный код.
- Кэш результатов в Postgres на 30 дней — реестр меняется медленно, а лимиты по запросам беречь надо.
- Если реестр недоступен, возвращай явный статус `unavailable`, не `not_found`. Разница принципиальная: «не нашли» и «не смогли проверить» — разные строки в отчёте.

**Приёмка этапа 2:** по трём реальным изделиям (одно зарегистрировано до марта 2025, одно после, одно несуществующее) функция возвращает корректный результат из правильного реестра.

---

## Этап 3. Мультимодальный вход

`services/gemini.py`. Gemini берёт изображения и аудио нативно — отдельный Whisper не нужен.

- Фото → распознать изделие, вернуть название, производителя если виден, тип
- Голос → транскрипт
- Файл (PDF, docx) → извлечь описание изделия
- Текст → как есть

Выход всегда один: `{product, qty, requirements[], raw_input}`. Создаётся `requests` со статусом `search` и уникальным токеном `RFQ-{год}-{счётчик}`.

Отдельная команда: пересылка любого приложенного файла на заданный в `.env` адрес через Gmail API, без всякой обработки.

**Приёмка этапа 3:** фото коробки изделия, голосовое и PDF дают одинаково пригодный `product`.

---

## Этап 4. Поиск и скрейпинг

`services/perplexity.py` — Search Query, ответ с источниками. Референс интеграции: https://github.com/membranedev/application-skills/tree/main/skills/perplexity (авторизация через Membrane CLI либо прямой вызов API — выбери прямой).

`services/firecrawl.py` — по каждому кандидату скрейпит сайт: наличие позиции, цена, email, телефон. Firecrawl только для сайтов поставщиков.

Если сайт держит антибот и Firecrawl не проходит — запасной вариант Botasaurus: https://github.com/omkarcloud/botasaurus (обходит Cloudflare, Turnstile, Datadome, есть ротация прокси и повторы).

Каждый найденный поставщик пишется в `suppliers` через upsert, каждая проверка — в `candidates`. **Перед формированием отчёта отфильтруй тех, кто в `blacklist` без `lifted_at`** — жёстким SQL-запросом, до ранжирования. Заблокированный поставщик не должен попадать в выдачу вообще, даже с лучшей ценой.

**Приёмка этапа 4:** по одному запросу в БД появляется 5–10 кандидатов с заполненными контактами и результатом проверки в реестре.

---

## Этап 5. Отчёт

`services/report.py`. Системную инструкцию владелец даст позже — положи её в `prompts/report.md` и читай оттуда, не хардкодь.

Жёсткое требование: **LLM получает готовый JSON кандидатов и возвращает JSON**, рендер сообщения делает код. Свободный текст от модели не принимается — номера РУ и цены поплывут не сразу, а на десятом прогоне.

В отчёте по каждому поставщику два независимых поля:
- `РУ на изделие` — из реестра: номер, держатель, статус, какой реестр
- `Поставщик заявляет наличие` — с сайта: да/нет, ссылка

Плюс контакты (email и телефон обязательны — они нужны на этапе 6), цена если найдена, флаги из unrega.

Отчёт уходит в Telegram, статус заявки → `awaiting_choice`.

**Приёмка этапа 5:** отчёт читается с телефона, ни одно поле не смешивает данные реестра и данные сайта.

---

## Этап 6. Голосовой выбор и накопление критериев

Владелец отвечает голосовым: кого выбрал и почему, почему не других. Либо просит дополнительную информацию по конкретному поставщику.

1. Gemini транскрибирует.
2. Вторая LLM по `prompts/criteria.md` извлекает структуру: список `{критерий, направление, вес, к какому поставщику относился}`.
3. Пишем в `criteria` (с `ON CONFLICT` по смыслу — если критерий уже есть, увеличиваем `times_seen` и обновляем `last_seen`) и в `criteria_events` вместе с сырым транскриптом.
4. Накопленные критерии подмешиваются в промпт отчёта на следующих заявках — вот в чём самопополняемость.

Транскрипт хранить обязательно: через полгода странный критерий надо будет чем-то объяснить.

Если владелец просит дополнительную информацию, а не выбирает — гоняем повторный Perplexity/Firecrawl по одному поставщику и возвращаемся в `awaiting_choice`.

**Приёмка этапа 6:** после трёх заявок в `criteria` лежат осмысленные строки, а не пересказ голосовых.

---

## Этап 7. Письмо и ответ

**Отправка.** `prompts/email.md` (владелец задаст позже) → LLM формирует письмо → Gmail API отправляет на адрес из `candidates`. Тема обязательно содержит токен: `[RFQ-2026-041] Запрос цены — <изделие>`. Сохраняем `gmail_thread` и `Message-ID` в `quote_requests`.

**Приём.** Без n8n: начни с опроса `history.list` раз в 3–5 минут, `users.watch` с Pub/Sub прикрутишь, когда заявок станет много.

Матчинг ответа с заявкой строго в этом порядке:
1. `In-Reply-To` / `References` → `Message-ID`
2. `threadId`
3. токен в теме
4. адрес отправителя — **последним**, он ненадёжен: отвечают из общей почты, через секретаря, с личного ящика

При совпадении: уведомление в Telegram («Ответил такой-то»), текст ответа, все вложения файлами в чат. Статус → `kp`.

**Приёмка этапа 7:** письмо, отправленное с другого адреса того же домена, корректно привязывается к заявке.

---

## Этап 8. Коммерческое предложение

Распакуй приложенный `kp-builder.skill` в `skills/kp-builder/`. Читай его SKILL.md — там правила, которые важнее удобства.

`services/kp.py`:
1. LLM извлекает из письма позиции и цены в JSON под формат `assets/kp.example.json`.
2. **Показываем владельцу извлечённые числа на подтверждение в Telegram.** Цены в письмах приходят с оговорками («без НДС», «от 10 штук», «при 100% предоплате») — неверно вытащенная цена уйдёт клиенту под вашей печатью.
3. После подтверждения — `python scripts/build_kp.py --data kp.json --out ...`, PDF в чат.

Черновик собирается с `--no-stamp`. Печать и подпись только на финальную версию — это правило скилла, не нарушай его автоматизацией.

Файлы `assets/logo.png`, `assets/stamp.png`, `assets/signature.png` и заполненный `assets/brand.json` кладёт владелец. Перед первой сборкой прогони `python scripts/check_assets.py`.

**Приёмка этапа 8:** из реального письма поставщика собирается КП с корректными суммами.

---

## Общие требования

- Все ключи в `.env`, в коде и в git ничего. `.env.example` с пустыми значениями — в репозиторий.
- Файлы `stamp.png` и `signature.png` — в `.gitignore`. Это фактически ключи от подписи документов.
- Весь SQL в `db/repo.py`. Ни одного запроса в хендлерах.
- Каждый внешний вызов (Gemini, Perplexity, Firecrawl, реестры, Gmail) — с таймаутом, ретраем и логированием стоимости запроса.
- Логи в файл с ротацией: по каждой заявке должно быть видно, что спросили у каждого источника и что он ответил.
- Тесты на `registry.py` и на матчинг ответных писем — это два места, где ошибка тихая и дорогая.
- Учёт расходов: каждый вызов платного API пишется в таблицу `api_calls` (сервис, токены, оценочная стоимость, request_id). При превышении `DAILY_API_BUDGET_USD` бот предупреждает владельца в чат и продолжает работу — не блокируется.
- Миграции через Alembic, а не руками. Откат должен работать.
- Ежедневный `pg_dump` в volume, хранить 14 копий.

## Порядок работы

Этапы 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8, строго по очереди. После каждого — показать владельцу работающий результат и дождаться подтверждения.

Если на этапе 2 gateway elk окажется закрыт или защищён — остановись и сообщи. Это меняет архитектуру: без автоматической проверки реестра придётся либо делать ручную проверку по ссылке в отчёте, либо искать выгрузку открытых данных. Не подменяй проверку реестра ответом LLM ни при каких обстоятельствах.
