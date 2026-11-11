#!/usr/bin/env bash
# ===========================================================================
#  Matrix Element Call Recorder — Nginx edition installer v3.1.0
#
#  Thin, loud launcher. All decisions live in installer/*.py; this file only
#  parses arguments, sanity-checks the environment, and turns any crash into a
#  readable report instead of "trap ERR at line N".
#
#  Modes:
#    --check      read-only: discovery, conflict report, installation plan
#    --install    install or upgrade the plugin
#    --repair     re-apply nginx routes, mounts and Element registration
#    --verify     end-to-end probe of every hop the record button uses
#    --doctor     deep diagnostics, no changes
#    --uninstall  remove containers, routes and UI module (keeps recordings)
# ===========================================================================
set -Eeuo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly HERE

R=$'\033[0m'; B=$'\033[1m'; C=$'\033[36m'; Y=$'\033[33m'; E=$'\033[31m'; G=$'\033[32m'
readonly R B C Y E G

PHASE=""
ANSWERS=""
ASSUME_YES=0

# --------------------------------------------------------------------------
# error reporting: command, exit code, line, and what to do about it
# --------------------------------------------------------------------------
LAST_CMD=""
trap 'LAST_CMD=$BASH_COMMAND' DEBUG

on_error() {
    local code=$1 line=$2 cmd=${3:-$LAST_CMD}
    printf '\n%sXX установка остановлена%s\n' "$E" "$R" >&2
    printf '   код возврата : %s\n' "$code" >&2
    printf '   строка       : %s\n' "$line" >&2
    printf '   команда      : %s\n' "$cmd" >&2
    printf '   скрипт       : %s\n' "${BASH_SOURCE[0]}" >&2
    printf '   каталог      : %s\n' "$PWD" >&2
    printf '\n  Ничего не удалено. Исправь причину и повтори ту же команду.\n' >&2
    printf '  Если непонятно — сними диагностику: bash install.sh --doctor %s\n\n' \
        "${ANSWERS:-<answers>}" >&2
    exit "$code"
}
trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR

usage() {
    cat <<'EOF'
Использование:
  bash install.sh <режим> <answers-файл>

Режимы:
  --check        Только проверка: окружение, конфликты, план установки.
                 Ничего не меняет. Код возврата 2 — есть блокеры.
  --install      Установка или обновление плагина.
  --repair       Переприменить маршруты nginx, монтирования и UI-модуль.
  --verify       Сквозная проверка всех звеньев цепочки кнопки записи.
  --doctor       Глубокая диагностика без изменений.
  --uninstall    Удалить контейнеры, маршруты и UI-модуль.
                 Записи и E2EE-профиль бота сохраняются.

Опции:
  -a, --answers <файл>   Путь к answers (иначе ./answers.env)
  -y, --yes              Неинтерактивный режим
  -h, --help             Эта справка

Примеры:
  cp answers.example /root/answers.env && chmod 600 /root/answers.env
  bash install.sh --check   /root/answers.env
  bash install.sh --install /root/answers.env
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check|--install|--repair|--verify|--doctor|--uninstall)
            PHASE="${1#--}"; shift ;;
        -a|--answers)
            [ $# -ge 2 ] || { printf '%sпосле %s нужен путь к файлу%s\n\n' "$E" "$1" "$R" >&2; usage; exit 2; }
            ANSWERS="$2"; shift 2 ;;
        --answers=*) ANSWERS="${1#*=}"; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --*) printf '%sнеизвестная опция: %s%s\n\n' "$E" "$1" "$R" >&2; usage; exit 2 ;;
        *)
            if [ -z "$ANSWERS" ]; then ANSWERS="$1"
            else printf '%sлишний аргумент: %s%s\n\n' "$E" "$1" "$R" >&2; usage; exit 2
            fi
            shift ;;
    esac
done

# Mode may also come from the answers file (INSTALL_ACTION=...).
read_answer() {
    local key="$1" file="$2"
    [ -f "$file" ] || return 0
    awk -F= -v k="$key" '
        /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
        {
            name=$1; gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
            if (name == k) {
                v = substr($0, index($0,"=")+1)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); gsub(/^["'"'"']|["'"'"']$/, "", v)
                print v
            }
        }' "$file" | tail -n 1
}

if [ -z "$ANSWERS" ] && [ -f "$HERE/answers.env" ]; then ANSWERS="$HERE/answers.env"; fi
if [ -z "$ANSWERS" ]; then
    printf '%sне указан answers-файл%s\n\n' "$E" "$R" >&2; usage; exit 2
fi
if [ ! -f "$ANSWERS" ]; then
    printf '%sanswers не найден: %s%s\n' "$E" "$ANSWERS" "$R" >&2
    printf '  cp answers.example /root/answers.env && chmod 600 /root/answers.env\n' >&2
    exit 1
fi
ANSWERS="$(cd -- "$(dirname -- "$ANSWERS")" && pwd)/$(basename -- "$ANSWERS")"

if [ -z "$PHASE" ]; then
    PHASE="$(read_answer INSTALL_ACTION "$ANSWERS")"
    PHASE="${PHASE:-check}"
fi
case "$PHASE" in
    check|install|repair|verify|doctor|uninstall) ;;
    reapply-ui) PHASE="repair" ;;
    *) printf '%sнеизвестный режим: %s%s\n\n' "$E" "$PHASE" "$R" >&2; usage; exit 2 ;;
esac

# The answers file holds bot passwords: never let it be world-readable.
if perms="$(stat -c '%a' "$ANSWERS" 2>/dev/null)"; then
    case "$perms" in
        600|400) ;;
        *) printf '%sanswers %s имеет режим %s — выполни: chmod 600 %s%s\n' \
               "$E" "$ANSWERS" "$perms" "$ANSWERS" "$R" >&2; exit 1 ;;
    esac
fi

if [ "$(id -u)" -ne 0 ]; then
    printf '%sзапускай от root%s (нужны docker socket и запись в конфиги Element Web)\n' \
        "$E" "$R" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    printf '%spython3 обязателен: apt-get install -y python3%s\n' "$E" "$R" >&2
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'; then
    printf '%sнужен python 3.8+, найден: %s%s\n' "$E" "$(python3 -V 2>&1)" "$R" >&2
    exit 1
fi

printf '%sMatrix Element Call Recorder — nginx plugin v3.1.0%s\n' "$B" "$R"
printf '  режим  : %s%s%s\n  answers: %s\n\n' "$C" "$PHASE" "$R" "$ANSWERS"

ARGS=(--phase "$PHASE" --answers "$ANSWERS")
[ "$ASSUME_YES" -eq 1 ] && ARGS+=(--yes)

# Not `exec`: if Python dies with a traceback we still want to print the
# context and the doctor hint below, instead of leaving a bare stack trace.
# `|| rc=$?` keeps the ERR trap out of the loop: install.py already prints its
# own diagnosis, and a duplicated report reads like two different failures.
rc=0
python3 "$HERE/installer/install.py" "${ARGS[@]}" || rc=$?

case "$rc" in
    0) exit 0 ;;
    2)  # --check found blockers; the report above is the whole message
        exit 2 ;;
    3)  # install refused by preflight, or doctor found blockers
        exit 3 ;;
    *)
        printf '\n%sXX режим %s завершился с кодом %s%s\n' "$E" "$PHASE" "$rc" "$R" >&2
        printf '  answers  : %s\n' "$ANSWERS" >&2
        printf '  каталог  : %s\n' "$HERE" >&2
        printf '  python   : %s\n' "$(python3 -V 2>&1)" >&2
        printf '\n  Что делать: посмотри сообщение выше. Если это трассировка Python —\n' >&2
        printf '  пришли её вместе с выводом: bash install.sh --doctor %s\n\n' "$ANSWERS" >&2
        exit "$rc" ;;
esac
