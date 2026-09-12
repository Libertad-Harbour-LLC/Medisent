# Готовые скиллы AI Skills под сервисы системы

Разбор каталога [aiskills.team](https://aiskills.team) по сервисам из ТЗ. Только раздел
скиллов — без шаблонов автоматизаций n8n/Make и без open-source проектов.

**Главное, что определяет чтение всего списка: скиллы работают в Claude Code, а не внутри
бота.** Бот — это Python-процесс на сервере, он ходит в API сам. Скилл — инструкция для
агента в сессии разработки. Ни один скилл ниже не «подключается к боту»: они либо помогают
написать нужный модуль правильно, либо дают паттерн, который переносится в код руками.

Скиллы через **Membrane CLI** (их в каталоге больше всего) требуют аккаунта Membrane и его
прослойки авторизации. Для рантайма бота это лишнее звено, но как справочник по API годятся.

Скачивание — раздел «Skills для Claude Code» на aiskills.team по тарифу аккаунта;
подключаются в папку `.claude/skills` проекта.

---

## 1. Telegram

| Скилл | Что даёт |
|---|---|
| **Разработка масштабируемых чат-ботов для Discord, Telegram и Slack** (`bot-developer`) | Архитектура production-бота: конечные автоматы для многоходовых диалогов с защитой от гонок, distributed rate limiting, event-driven структура. [источник](https://github.com/curiositech/some_claude_skills/tree/main/.claude/skills/bot-developer) |
| **Telegram — автоматизация сообщений и чатов** (Membrane) | Отправка сообщений, работа с чатами, прокси-запросы к Telegram API. [источник](https://github.com/membranedev/application-skills/tree/main/skills/telegram) |

Под aiogram 3, приём голосовых и скачивание файлов через Bot API готового скилла нет —
пишется руками. Скилл на GramJS из каталога не подходит: это MTProto, а не Bot API.

## 2. Gemini

Каталог по Gemini почти целиком про генерацию изображений — не наш случай. Подходящее:

| Скилл | Что даёт |
|---|---|
| **Firebase AI Logic — интеграция Gemini в приложения** | Мультимодальный ввод (текст, изображения, аудио, видео, PDF) и структурированный JSON-вывод в одном месте. Ближе всего к этапу 3. [источник](https://github.com/Cukurikik/Omni/tree/main/.claude/skills/firebase-ai-logic) |
| **Извлечение текста и структуры из документов** (`document-extract`) | OCR и разбор структуры сканов, PDF и фотографий, confidence-отчёт. Под ветку «файл → описание изделия». [источник](https://github.com/davidroliverba/architectkb/tree/main/.claude/skills/document-extract) |
| **Генерация пошаговых инструкций из скриншотов** (`tutorial-generator`) | Рабочий пример вызова Gemini Vision API из Python — как референс кода. [источник](https://github.com/minicoohei/ai-agent-camp/tree/main/.claude/skills/tutorial-generator) |
| **Google Cloud Vision**, **Cloudmersive** (Membrane) | Запасной путь для распознавания фото и OCR. |

Скилла под транскрибацию голосовых через Gemini в каталоге нет.

## 3. Perplexity

| Скилл | Что даёт |
|---|---|
| **Perplexity — веб-поиск с пятью режимами** (`perplexity-search`) | Прямой вызов API, режимы `--search`/`--research`/`--reason`/`--deep` на моделях sonar, фильтры по свежести и доменам. Лучший вариант под поиск поставщиков. [источник](https://github.com/parcadei/Continuous-Claude-v3/tree/main/.claude/skills/perplexity-search) |
| **Perplexity — поиск и диалог через AI** (Membrane) | Search Query, продолжение диалога, коллекции. Тот, что указан в ТЗ. [источник](https://github.com/membranedev/application-skills/tree/main/skills/perplexity) |
| **Perplexity — поиск с учётом базы знаний** (`perplexity-research`) | Отдаёт дельту «что нового против уже известного». Под повторную проверку поставщика на этапе 6. [источник](https://github.com/garrytan/gbrain/blob/master/.claude/skills/perplexity-research/SKILL.md) |

## 4. Firecrawl и скрейпинг

| Скилл | Что даёт |
|---|---|
| **Веб-агент для извлечения структурированных данных** (`research-agent`) | Поиск URL → Firecrawl → извлечение полей в JSON, с валидацией по эталону и доводкой до 90% точности. Ровно задача «сайт поставщика → наличие, цена, контакты». [источник](https://github.com/LeadGrowGTM/research-process-builder/tree/master/.claude/skills/research-agent) |
| **Firecrawl — краулинг и SEO-аудит сайтов** (`seo-firecrawl`) | `crawl`, `map`, `scrape`, `search`, рендер JS для SPA. [источник](https://github.com/AgriciDaniel/claude-seo/tree/main/extensions/firecrawl/skills/seo-firecrawl) |
| **Firecrawl — сбор данных с веб-страниц** (`firecrawl-scrape`) | Минимальная обёртка: markdown/html/text. [источник](https://github.com/parcadei/Continuous-Claude-v3/tree/main/.claude/skills/firecrawl-scrape) |
| **Firecrawl — парсинг и обход сайтов** (Membrane) | [источник](https://github.com/membranedev/application-skills/tree/main/skills/firecrawl) |
| **Сбор сайта и соцсетей компании по названию** (`business-contact-social-links-skill`) | По названию компании находит официальный сайт и контакты. Когда Perplexity дал название без домена. [источник](https://github.com/browser-act/skills/tree/main/solutions/lead-generation/business-contact-social-links-skill) |

Под антибот вместо open-source Botasaurus есть скиллы-сервисы: **ZenRows** (обход антибота,
прокси, геолокация), **ScrapingBee**, **Scrapingdog**, **Zenscrape**, **ScrapingBot**,
**Zyte API** — все через Membrane.

## 5. Реестры Росздравнадзора

Готового скилла нет. Косвенно помогают:

| Скилл | Что даёт |
|---|---|
| **Интеграция REST, GraphQL и WebSocket API** (`api-integrating`) | Типизация, ретраи, управление ключами. [источник](https://github.com/frvnkfrmchicago/skills-library-v2/tree/main/.claude/skills/api-integrating) |
| **Реверс клиентской подписи запросов** (`client-reverse`) | Понадобится, только если gateway `elk` окажется за подписью запросов. Брать как справочник по DevTools-трассировке. [источник](https://github.com/shuvonsec/claude-bug-bounty/tree/main/skills/client-reverse) |

Скиллы разведки и сканирования уязвимостей из того же набора (`web2-recon`) сознательно не
рекомендуются: сайт госоргана, задача — читать открытые данные.

## 6. LLM для отчёта и письма

| Скилл | Что даёт |
|---|---|
| **Разработка и оптимизация промптов для LLM** (`Prompt Engineer`) | Строгий JSON-вывод, function calling, гардрейлы, оценочные наборы. Прямо под `prompts/report.md` и `prompts/email.md`. [источник](https://github.com/Jeffallan/claude-skills/tree/main/skills/prompt-engineer) |
| **Проектирование чётких инструкций для LLM** (`prompt-architect`) | Превращает размытое намерение в проверяемую инструкцию, с fallback при отсутствии данных. [источник](https://github.com/repowise-dev/claude-code-prompts/tree/master/skills/prompt-architect) |
| **Защита LLM от инъекций и джейлбрейков** (`prompt-guard`) | Классификатор Meta Prompt-Guard-86M на входе: фильтрует данные от сторонних API и скрейпинга. В систему втекают письма незнакомых поставщиков и содержимое их сайтов. [источник](https://github.com/Orchestra-Research/AI-Research-SKILLs/tree/main/07-safety-alignment/prompt-guard) |

## 7. Gmail

Самый богатый раздел каталога.

| Скилл | Что даёт |
|---|---|
| **Управление Gmail через CLI** (`gog-gmail`) | 20+ команд: поиск, отправка, ответы, пересылка, вложения, метки, **история**, сырой ответ Gmail API. Флаги `--readonly`, `--dry-run`, `--wrap-untrusted`. Покрывает и отправку, и приём. [источник](https://github.com/openclaw/gogcli/tree/main/.agents/skills/gog-gmail) |
| **Gmail — управление почтой в агентском пайплайне** (Membrane) | 20+ операций, треды и вложения. [источник](https://github.com/membranedev/application-skills/tree/main/skills/gmail) |
| **Разбор входящих из Gmail и Slack** (`check-inbox`) | Паттерн обёртки внешнего контента в `<external_untrusted_content>`. [источник](https://github.com/minicoohei/ai-agent-camp/tree/main/.claude/skills/check-inbox) |
| **Триаж входящей почты Gmail** (`gog-inbox-triage`) | Санитизация тредов через `--sanitize-content --wrap-untrusted`, никогда не отправляет письма сам. [источник](https://github.com/openclaw/gogcli/tree/main/.agents/skills/gog-inbox-triage) |
| **Сохранение вложений из Gmail** (`gog-save-attachments`) | Обращается с каждым вложением как с недоверенным контентом. Под пересылку файлов. [источник](https://github.com/openclaw/gogcli/tree/main/.agents/skills/gog-save-attachments) |
| **Отслеживание писем без ответа** (`gmail-takip-sistemi`) | Находит отправленные без ответа и готовит follow-up. Ложится на статус `silent` в `quote_requests`. [источник](https://github.com/komunite/kalfa/tree/main/.claude/skills/ai-automation/gmail-takip-sistemi) |
| **gmail-adapter**, **inbox-reader** | Простая пара «отправка через OAuth2» и «чтение и поиск», если нужен минимум. |

Скилла, который делает матчинг ответа по `In-Reply-To`/`References`, нет — это наш код и
наши тесты.

## 8. PostgreSQL

| Скилл | Что даёт |
|---|---|
| **Проектирование PostgreSQL: схемы, индексы, миграции** (`postgres-database`) | Alembic с `upgrade`/`downgrade`, **upsert через `ON CONFLICT`**, частичные и GIN-индексы. Совпадает с нашей схемой один в один. [источник](https://github.com/cohen-liel/hivemind/tree/main/.claude/skills/postgres-database) |
| **Безопасные миграции Alembic** (`alembic-best-practices`) | Чеклист опасных операций, ловит **пустой `downgrade()`** — ТЗ требует рабочего отката. [источник](https://github.com/baekenough/oh-my-customcode/tree/develop/.claude/skills/alembic-best-practices) |
| **Проектирование схем PostgreSQL** (`postgresql`) | Разбирает поведение `UNIQUE` с NULL. Критично: оба наших уникальных индекса частичные. [источник](https://github.com/wshobson/agents/tree/main/plugins/database-design/skills/postgresql) |
| **Миграции PostgreSQL — паттерны** (`postgres-migrations`) | Идемпотентность, `pg_trgm`, полнотекстовый поиск. [источник](https://github.com/pr-pm/prpm/tree/main/.claude/skills/postgres-migrations) |
| **PostgreSQL — оптимизация и администрирование** (`Postgres pro`) | EXPLAIN ANALYZE, расширения включая **pgvector**, VACUUM. Пригодится на этапе 11. [источник](https://github.com/Jeffallan/claude-skills/tree/main/skills/postgres-pro) |
| **Пакетная вставка через psycopg2** (`psycopg2-batch-insert-optimization`) | `execute_values` плюс `ON CONFLICT DO UPDATE`. Под пакетную запись кандидатов. [источник](https://github.com/divinevideo/divine-mobile/tree/main/.claude/skills/psycopg2-batch-insert-optimization) |

## 9. Mathesar

Скилла под Mathesar нет. Есть скиллы под альтернативы, с оговоркой: они позволяют **агенту**
управлять этими системами через API, а не заменяют развёртывание веб-админки. Mathesar по ТЗ
ставится в Docker и используется владельцем руками — скилл для этого не нужен.

**NocoDB**, **Baserow**, **PostgREST**, **Grist** (все Membrane), **dbx-studio** —
если решите взять что-то вместо Mathesar.

## 10. PDF и kp-builder

kp-builder уже в репозитории и закрывает сборку КП. Из каталога полезно другое:

| Скилл | Что даёт |
|---|---|
| **Комплексная обработка PDF-документов** (`pdf`) | pypdf, pdfplumber, извлечение **таблиц**, OCR. Чего в kp-builder нет: поставщики присылают прайсы PDF-вложением, на этапе 8 их надо читать. [источник](https://github.com/OpenCoworkAI/open-cowork/tree/main/.claude/skills/pdf) |
| **Генерация PDF-отчёта по GEO-аудиту** (`geo-report-pdf`) | Конвейер HTML → headless Chrome → PDF с автоматическими разрывами страниц. Ближайший аналог нашего рендера. [источник](https://github.com/zubair-trabzada/geo-seo-claude/tree/main/skills/geo-report-pdf) |
| **Академический постер в HTML** (`paper-poster-html`) | Экспорт HTML/CSS в PDF через **Playwright и headless Chromium**. [источник](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/tree/main/skills/paper-poster-html) |
| **visibly-seo-pdf-build** (fpdf2), **APITemplate.io** (Membrane) | Запасные пути, если Chromium окажется тяжёл. |

## 11. База критериев

| Скилл | Что даёт |
|---|---|
| **Семантический поиск по базе знаний** (`recall`) | **PostgreSQL плюс BGE-эмбеддинги**, режимы гибридный / только вектор / только текст. Лучший вариант: не требует новой инфраструктуры, ложится на ту же Postgres. [источник](https://github.com/parcadei/Continuous-Claude-v3/tree/main/.claude/skills/recall) |
| **Проектирование RAG-систем** (`rag-architect`) | Выбор между pgvector, Qdrant, Chroma; гибридный поиск Vector + BM25. Указан в ТЗ. [источник](https://github.com/Jeffallan/claude-skills/tree/main/skills/rag-architect) |
| **AgentDB — память о стратегиях агента** (`reasoningbank-agentdb`) | Трекинг траекторий, дистилляция памяти, паттерны успеха и неуспеха. Указан в ТЗ. [источник](https://github.com/ruvnet/ruflo/tree/main/.claude/skills/reasoningbank-agentdb) |
| **agentdb-vector-search**, **agentdb-memory-patterns**, **agent-memory**, **add-mnemon** | Остальная линейка памяти агента. |

## 12. Инфраструктура

| Скилл | Что даёт |
|---|---|
| **Контейнеризация Python-сервисов с Docker** (`docker-deployment`) | Multi-stage на `python:3.12-slim`, непривилегированный пользователь, healthcheck, **compose с Postgres 16**, `.env.example`, Makefile. Практически готовый этап 0. [источник](https://github.com/cohen-liel/hivemind/tree/main/.claude/skills/docker-deployment) |
| **Лучшие практики Docker и docker-compose** (`docker-best-practices`) | Секреты через BuildKit, пиннинг образов по digest, именованные тома, лимиты ресурсов, healthchecks. [источник](https://github.com/baekenough/oh-my-customcode/tree/develop/.claude/skills/docker-best-practices) |
| **Управление Docker контейнерами и Compose стеками** (`docker-management`) | Жизненный цикл, отладка падающих контейнеров, справочная таблица команд. [источник](https://github.com/NousResearch/hermes-agent/tree/main/optional-skills/devops/docker-management) |
| **Анализ и ротация логов** (`review-logs`) | Ротация по возрасту и размеру. Оценку через локальную Ollama придётся отрезать, механика переносится. [источник](https://github.com/sliamh11/Deus/tree/main/.claude/skills/review-logs) |

Скилла под ежедневный `pg_dump` с хранением 14 копий в каталоге нет.

## 13. Docker и публикация для человека без опыта

Требование владельца: он с Docker не работал ни разу, нужна отдельная пошаговая инструкция —
как упаковать бота в контейнер и как его опубликовать.

**Важно понимать разницу.** Все скиллы из раздела 12 — агентские: они заставляют Claude
писать правильные Dockerfile и compose. Человека они не учат ничему, читать их бесполезно.
Учебника по Docker в каталоге нет вообще. Зато есть скиллы, которые **производят**
человекочитаемую инструкцию — вот они и нужны:

| Скилл | Что даёт |
|---|---|
| **Генератор технических туториалов** (`dev-guide-generator`) | Главный под эту задачу. Конвейер из четырёх фаз: определение аудитории и её уровня, чеклист пререквизитов с разделением «обязательно знать» и «желательно», OS-специфичные команды установки **с проверкой результата**, 5–10 основных шагов, каждый с верификацией, плюс разбор типичных ошибок и итоговая шпаргалка. Реальные ключи и пароли в вывод не попадают. [источник](https://github.com/zebbern/claude-code-guide/tree/main/skills/dev-guide-generator) |
| **Создание и обновление runbook** (`runbook`) | Задаёт форму эксплуатационной инструкции: каждый шаг — точная команда и **ожидаемый вывод**, а не проза. Ровно то, что нужно новичку: видно, совпало у тебя или нет. [источник](https://github.com/testdouble/han/tree/main/han-documentation/skills/runbook) |
| **Написание технической документации** (`documentation`) | README, runbook'и, onboarding-гайды; быстрый старт до первого результата за 5 минут, описание шагов отката и путей эскалации. [источник](https://github.com/anthropics/knowledge-work-plugins/tree/main/engineering/skills/documentation) |
| **Генерация пользовательских гайдов по Diataxis** (`user-guides`) | Task-oriented how-to с обязательным шагом верификации; отделяет туториал от справочника — новичку нужен туториал, а не справочник. [источник](https://github.com/littlebearapps/pitchdocs/tree/main/.claude/skills/user-guides) |
| **Операционный runbook для дежурных** (`eng-runbook`) | Одностраничник с готовыми к копированию командами и чеклистом реагирования. Формат «что делать, когда бот упал ночью». [источник](https://github.com/nexu-io/html-anything/tree/main/next/src/lib/templates/skills/eng-runbook) |
| **DevOps-инженер для автоматизации деплоя** (`Devops engineer`) | Прямо заявлен сценарий «нужно контейнеризировать приложение и не знаешь, с чего начать»: Dockerfile, CI/CD, план отката, явное подтверждение перед развёртыванием. [источник](https://github.com/Jeffallan/claude-skills/tree/main/skills/devops-engineer) |
| **Docker Hub — управление образами** (Membrane) | Половина «как опубликовать»: репозитории, теги образов. [источник](https://github.com/membranedev/application-skills/tree/main/skills/docker-hub) |
| **Fly.io — управление облачной инфраструктурой** (Membrane) | Если хостинг будет managed: приложения, машины, тома, секреты, домены и сертификаты. [источник](https://github.com/membranedev/application-skills/tree/main/skills/flyio) |
| **crafting-effective-readmes**, **doc-coauthoring** | Шаблоны README под аудиторию и совместная доводка текста. |

**Вывод по разделу:** скилл сам по себе инструкцию не заменит. Связка простая —
`dev-guide-generator` задаёт структуру туториала, `runbook` — формат «команда плюс ожидаемый
вывод», и по ним пишется `docs/DEPLOY.md` под конкретный выбранный хостинг. Это deliverable
этапа 0, а не то, что владелец должен вычитывать из чужих скиллов.

---

## Чего в каталоге нет вовсе

Реестры Росздравнадзора, матчинг ответных писем по заголовкам, aiogram 3 с приёмом голосовых,
транскрибация через Gemini, бэкапы Postgres, Mathesar, учебник по Docker для человека.
Показательно, что это ровно те места, которые ТЗ называет критичными, — ядро системы пишется
руками в любом случае, скиллы закрывают периметр.

## Что ставить сейчас

Под ближайшие этапы хватает пяти: `docker-deployment` и `docker-best-practices` (этап 0),
`postgres-database` и `alembic-best-practices` (этап 1), `api-integrating` (этап 2).
Плюс `dev-guide-generator` и `runbook` — под инструкцию для владельца.

Остальное — по мере подхода к этапу. Иначе описания двух десятков скиллов будут занимать
контекст в каждой сессии.
