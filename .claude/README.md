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
