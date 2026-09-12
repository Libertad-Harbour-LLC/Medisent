# `.claude/` — инструменты разработки, не часть бота

Содержимое этой папки Claude Code подхватывает автоматически в каждой сессии по репозиторию.
К Telegram-боту подбора поставщиков (`docs/SPEC.md`) оно отношения не имеет и в рантайм бота
не попадает.

## Skill Forge

Конструктор скиллов для Claude Code: спланировать, собрать, проверить, прогнать эval,
опубликовать и портировать скилл на другие платформы.

- Источник: https://github.com/AgriciDaniel/skill-forge
- Версия: `1.0.0`, коммит `2872ee9e1be8b81d48d8e6f2fe6c96225885e87b` от 2026-04-10
- Лицензия: MIT, © 2026 Daniel Agrici — текст в `skills/skill-forge/LICENSE`

Штатный `install.sh` из апстрима копирует всё в `~/.claude/`, то есть на уровень пользователя.
В удалённых сессиях Claude Code домашняя директория живёт только до конца сессии, поэтому
здесь сделана установка уровня репозитория: те же файлы лежат в `.claude/` и едут вместе с git.

### Что установлено

| Что | Куда |
|---|---|
| Главный скилл-оркестратор | `skills/skill-forge/` (SKILL.md, references/, scripts/, assets/) |
| 8 подскиллов | `skills/skill-forge-{plan,build,review,evolve,eval,benchmark,publish,convert}/` |
| 8 субагентов | `agents/skill-forge-*.md` |

### Как пользоваться

```
/skill-forge                      интерактивный мастер
/skill-forge plan <домен>         архитектура и выбор уровня сложности (1–4)
/skill-forge build <имя>          сборка структуры скилла
/skill-forge review <путь>        аудит скилла, оценка 0–100
/skill-forge evolve <путь>        доработка по обратной связи
/skill-forge eval <путь>          прогон eval-набора
/skill-forge benchmark <путь>     замер с анализом разброса
/skill-forge publish <путь>       упаковка в .skill
/skill-forge convert <путь>       порт на Codex, Gemini CLI, Antigravity, Cursor
```

Скрипты запускаются и напрямую, зависимостей кроме стандартной библиотеки Python 3.10+ нет:

```bash
python3 .claude/skills/skill-forge/scripts/validate_skill.py <путь-к-скиллу>
python3 .claude/skills/skill-forge/scripts/init_skill.py <имя> --tier 2
```

### Обновление и удаление

Обновить — перекопировать `skill-forge/`, `skills/skill-forge-*/` и `agents/skill-forge-*.md`
из свежего клона апстрима сюда и обновить коммит в этом файле. Удалить — стереть
`.claude/skills/skill-forge*` и `.claude/agents/skill-forge-*.md`; `install.sh --uninstall`
из апстрима сюда не смотрит, он чистит `~/.claude/`.

### Что было проверено перед установкой

Сетевых вызовов в скриптах нет, деструктивных операций нет (два `chmod 0o755` на
сгенерированные install-скрипты), субагенты ограничены `Read` и `Grep`, посторонних
инструкций в markdown нет. Главный скилл запрашивает `Bash`, `Write`, `Edit` и `WebFetch` —
это нужно ему для генерации файлов скиллов.

## Скиллы, поставленные вне каталога AI Skills

Происхождение скиллов из каталога — в [`docs/skills.md`](../docs/skills.md), там у каждого
ссылка на апстрим. Ниже — то, что из ссылки не видно: точные коммиты последней партии,
лицензии и оговорки, которые надо знать до того, как скилл дёрнут в работе.

Все десять скопированы папками из апстрима как есть; единственная правка — поле `name`
у трёх скиллов AgentDB, см. ниже. В каждую папку добавлен `LICENSE` из корня
исходного репозитория — в апстриме он лежит уровнем выше и при копировании папки терялся.

| Скилл | Апстрим | Коммит | Лицензия |
|---|---|---|---|
| `business-contact-social-links-skill` | browser-act/skills | `11c057b` от 2026-08-24 | MIT, © 2026 BrowserAct |
| `psycopg2-batch-insert-optimization` | divinevideo/divine-mobile | `5574d52` от 2026-09-12 | **MPL-2.0** |
| `paper-poster-html` | wanshuiyin/Auto-claude-code-research-in-sleep | `f1bd907` от 2026-09-11 | MIT, © 2026 wanshuiyin (+ вендоренный posterly, MIT) |
| `reasoningbank-agentdb`, `agentdb-vector-search`, `agentdb-memory-patterns` | ruvnet/ruflo | `b02c0ca` от 2026-09-12 | MIT, © 2024-2026 ruvnet |
| `docker-management` | NousResearch/hermes-agent | `1c671be` от 2026-09-12 | MIT, © 2025 Nous Research |
| `eng-runbook` | nexu-io/html-anything | `c312045` от 2026-08-23 | Apache-2.0 |
| `pytest`, `requests` | microsoft/debugpy | `e220805` от 2026-09-01 | MIT, © Microsoft |

`psycopg2-batch-insert-optimization` под MPL-2.0: правки в этом файле остаются под MPL и
исходник должен быть доступен. Форкать его внутрь своего кода нельзя — паттерн из него
переносится в `db/repo.py` руками, сам файл остаётся справочником.

### Оговорки по конкретным скиллам

**`business-contact-social-links-skill` — единственный из десяти с исполняемым скриптом.**
`scripts/business_contact_social_links.py` ходит в платный сервис BrowserAct
(`api.browseract.com`), ключ берёт из `BROWSERACT_API_KEY`, без ключа сразу выходит.
Таймауты выставлены, деструктивных операций нет, ключ никуда кроме заголовка `Authorization`
не уходит. Для бота это ещё один платный внешний сервис поверх Perplexity и Firecrawl —
прежде чем закладывать его в этап 4, решение по стоимости за владельцем.

**Три скилла AgentDB тянут чужой стек.** Им нужны Node.js 18+, пакет `npx agentdb@latest`
(SQLite-база векторов) и регистрация своего MCP-сервера в Claude Code; примеры на TypeScript,
эмбеддинги — через OpenAI API. Это против зафиксированного стека: у нас Python и PostgreSQL,
а под семантический поиск уже выбран `recall` на той же базе. Брать из них паттерны
(трекинг траекторий, дистилляция памяти, вердикт по исходу) — да; ставить AgentDB как
инфраструктуру — нет, это отдельное решение владельца.
Апстрим объявляет их через `name: "ReasoningBank with AgentDB"` и т.п. — не kebab-case и не
совпадает с именем папки, из-за чего скилл не проходит валидацию и может не загрузиться.
Поле `name` во всех трёх исправлено на имя папки; при обновлении из апстрима правку
придётся повторить.

**`paper-poster-html` — тяжёлый и не полностью применимый.** 32 файла, свои гейты качества,
шаблоны постеров и токены под ICML/NeurIPS/ICLR. Ценность для нас одна: рабочий конвейер
HTML → Playwright → печатный PDF, тот же, что в `kp-builder`. Требует `Bash(*)` и
`mcp__codex__codex` — этого MCP-сервера в проекте нет, так что фазы кросс-модельного ревью
не отработают; ссылку на `../shared-references/taste-calibration.md` апстрим тоже не
приложил. Сетевых вызовов в скриптах нет, `subprocess` — только запуск своих же гейтов
и Chromium.

**`eng-runbook` — не скилл, а шаблон страницы.** Пришёл из генератора HTML-страниц, описание
и тело на китайском, `SKILL.md` в 21 строку — это спецификация вёрстки, а не процедура.
Пользоваться стоит `example.html`: готовая форма однодневного runbook (таблица алертов,
блоки команд с копированием, чеклист реагирования). Как форма для `docs/DEPLOY.md` годится,
как инструкция агенту — нет.

**`pytest` и `requests` — короткие чеклисты хороших практик**, не из каталога AI Skills, а из
`.claude/skills` самого debugpy. `requests` прямо ложится на правило проекта «каждый внешний
вызов с таймаутом и ретраем»: `Session` + `HTTPAdapter` + `urllib3.util.Retry`, обязательный
`raise_for_status()`, запрет `verify=False`. `pytest` дополняет уже стоящий `pytest-patterns`.

### Что было проверено перед установкой (десять скиллов)

Каждый прочитан целиком. Скан на сетевые вызовы, `subprocess`, `os.system`, удаление файлов,
`eval`/`exec`, обращения к `.env`, `~/.ssh` и посторонние инструкции вида «ignore previous
instructions» — чисто. Исполняемый код есть только у двух: у BrowserAct (разобран выше) и у
`paper-poster-html` (6300 строк Python, сеть не трогают). Остальные восемь — чистый markdown.
Валидатор Skill Forge проходит на всех десяти:

```bash
python3 .claude/skills/skill-forge/scripts/validate_skill.py .claude/skills/<имя>
```
