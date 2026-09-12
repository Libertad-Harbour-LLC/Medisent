#!/usr/bin/env bash
# Ежедневный дамп базы. Хранится 14 копий (требование ТЗ).
#
# Локально запускается из cron или вручную: ./scripts/backup.sh
# В Railway у managed-базы есть свои бэкапы, но они не заменяют этот дамп:
# если платформу придётся менять, переносить будем именно его.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP="${KEEP:-14}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL не задан" >&2
  exit 1
fi

# pg_dump не понимает драйвер SQLAlchemy — срезаем +asyncpg.
DUMP_URL="${DATABASE_URL/postgresql+asyncpg:\/\//postgresql://}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/medisent-$STAMP.sql.gz"

echo "Дамп в $OUT"
pg_dump --no-owner --no-privileges "$DUMP_URL" | gzip -9 > "$OUT"

SIZE="$(du -h "$OUT" | cut -f1)"
echo "Готово, размер $SIZE"

# Держим только последние $KEEP файлов.
COUNT="$(find "$BACKUP_DIR" -name 'medisent-*.sql.gz' -type f | wc -l)"
if (( COUNT > KEEP )); then
  find "$BACKUP_DIR" -name 'medisent-*.sql.gz' -type f -printf '%T@ %p\n' \
    | sort -n | head -n "$(( COUNT - KEEP ))" | cut -d' ' -f2- \
    | while read -r old; do echo "Удаляю старый дамп $old"; rm -f "$old"; done
fi

echo "Копий в $BACKUP_DIR: $(find "$BACKUP_DIR" -name 'medisent-*.sql.gz' | wc -l)"
