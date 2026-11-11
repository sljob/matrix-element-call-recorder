#!/usr/bin/env bash
# ===========================================================================
#  Post-install verification. Read-only, exit code is meaningful:
#    0  every hop of the record-button chain answers
#    1  something is broken (details on stdout)
#    2  usage/environment problem
#
#  Safe to run from cron or CI: it never writes, reloads or restarts anything.
#
#  Usage: bash tools/verify.sh [base-url] [answers-file]
#    base-url  default http://127.0.0.1:18085
# ===========================================================================
set -Eeuo pipefail

BASE="${1:-http://127.0.0.1:18085}"
ANSWERS="${2:-}"
FAIL=0
R=$'\033[0m'; G=$'\033[32m'; Y=$'\033[33m'; E=$'\033[31m'; B=$'\033[1m'

pass() { printf '%s  ok  %s%s\n' "$G" "$1" "$R"; }
fail() { printf '%s  FAIL %s%s\n' "$E" "$1" "$R"; FAIL=1; }
warn() { printf '%s  warn %s%s\n' "$Y" "$1" "$R"; }

code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-8}" "$1" 2>/dev/null || echo 000
}

expect() {
    local label="$1" url="$2" want="$3"
    local got
    got="$(code "$url")"
    if [ "$got" = "$want" ]; then pass "$label -> $got"; else fail "$label -> $got (want $want)"; fi
}

printf '%sПроверка цепочки кнопки записи%s\n' "$B" "$R"
printf '  база: %s\n\n' "$BASE"

# 1. nginx liveness — must not touch the controller
expect "nginx /healthz" "$BASE/healthz" 200

# 2. static UI
expect "UI  /recording/" "$BASE/recording/" 200
js="$(code "$BASE/recording/record-button.js")"
if [ "$js" = 200 ]; then
    if curl -s --max-time 8 "$BASE/recording/record-button.js" | grep -q 'recording'; then
        pass "UI  record-button.js -> 200 (содержимое похоже на кнопку)"
    else
        fail "UI  record-button.js -> 200, но содержимое не похоже на кнопку"
    fi
else
    fail "UI  record-button.js -> $js"
fi

# 3. redirect normalisation
redir="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/recording" || echo 000)"
case "$redir" in
    301|308) pass "UI  /recording -> $redir (редирект на /recording/)";;
    *) fail "UI  /recording -> $redir (ожидался 301/308)";;
esac

# 4. controller through nginx — the hop that used to be 502 forever
expect "API /recording/api/health"  "$BASE/recording/api/health"  200
expect "API /recording/api/version" "$BASE/recording/api/version" 200

# 5. controller health must not claim workers while reporting them dead
health="$(curl -s --max-time 8 "$BASE/recording/api/health" || true)"
if [ -n "$health" ]; then
    if printf '%s' "$health" | grep -q '"worker_unavailable":true'; then
        warn "API контроллер жив, но worker недоступен — start/stop будут 5xx"
        printf '        диагностика: %s\n' \
            "$(printf '%s' "$health" | tr ',' '\n' | grep -i diagnos -A1 | head -2 | tr '\n' ' ')"
    else
        pass "API worker доступен (worker_unavailable=false)"
    fi
    if printf '%s' "$health" | grep -q '"synapse_configured":false'; then
        warn "API SYNAPSE_URL не задан — проверка прав вернёт 503"
    fi
fi

# 6. auth gate: an anonymous start must be refused, not attempted
sc="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
      -X POST -H 'Content-Type: application/json' \
      -d '{"roomId":"!verify:local"}' \
      "$BASE/recording/api/record/start" 2>/dev/null || echo 000)"
case "$sc" in
    401|403) pass "API POST /start без токена -> $sc (доступ закрыт корректно)";;
    502|504) fail "API POST /start -> $sc (контроллер недоступен)";;
    000)     fail "API POST /start -> нет ответа";;
    *)       warn "API POST /start без токена -> $sc (ожидался 401/403)";;
esac

# 7. recordings portal, when published
if [ -n "${ANSWERS:-}" ] && [ -f "$ANSWERS" ]; then
    portal_port="$(awk -F= '/^REC_HTTP_PORT=/{gsub(/"/,"",$2);print $2}' "$ANSWERS" | tail -1)"
    if [ -n "${portal_port:-}" ]; then
        pc="$(code "http://127.0.0.1:${portal_port}/healthz" 5)"
        case "$pc" in
            200) pass "portal :${portal_port}/healthz -> 200";;
            *)   warn "portal :${portal_port} -> ${pc} (не критично для кнопки)";;
        esac
    fi
fi

printf '\n'
if [ "$FAIL" -eq 0 ]; then
    printf '%sЦепочка в порядке.%s Открой Element Web и перезагрузи страницу:\n' "$G$B" "$R"
    printf '  кнопка записи появится в заголовке комнаты (power >= 50).\n\n'
    exit 0
fi
printf '%sЕсть неработающие звенья.%s Сними диагностику:\n' "$E$B" "$R"
printf '  bash install.sh --doctor %s\n\n' "${ANSWERS:-<answers.env>}"
exit 1
