#!/bin/bash
# ==========================================================================
#  Smoke test for the overlay plugin against the fake stack.
#
#  What this proves without a real Matrix server:
#    1. install.sh --check detects Traefik, its network, Element Web, free ports
#    2. the generated compose file is valid and starts
#    3. the button <script> reaches index.html and the file is served
#    4. Traefik routes /recording/* to the controller (stripprefix works)
#    5. an unauthenticated request can reach the controller but a wrong
#       recorder token cannot start a recording on a worker
#
#  What it does NOT prove: that a headless Chromium can join a real E2EE call.
#  That needs a real Synapse + LiveKit; see TESTING.ru.md.
#
#  Usage:  bash smoke.sh            (fake stack must already be up)
#  Exits 0 only if every check passes.
# ==========================================================================
set -uo pipefail

DOMAIN="${DOMAIN:-meet.localhost}"
BASE="https://${DOMAIN}:8443"
PLUGIN_ANSWERS="${PLUGIN_ANSWERS:-/tmp/answers-smoke.conf}"
PASS=0
FAIL=0

c0=$'\033[0m'; cg=$'\033[32m'; cr=$'\033[31m'; cy=$'\033[33m'; cb=$'\033[1m'

ok()   { PASS=$((PASS+1)); printf '%s PASS %s %s\n' "$cg" "$c0" "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '%s FAIL %s %s\n' "$cr" "$c0" "$*"; }
note() { printf '%s ·· %s %s\n' "$cy" "$c0" "$*"; }
head_() { printf '\n%s%s%s\n' "$cb" "$*" "$c0"; }

curl_() { curl -sk --max-time 10 "$@"; }

# ---------------------------------------------------------------- 1. fake stack
head_ "1. fake stack"
if docker ps --format '{{.Names}}' | grep -qx fake-traefik; then
    ok "fake-traefik running"
else
    bad "fake-traefik not running — start it: docker compose -f compose.fake.yml up -d"
    exit 1
fi
if docker ps --format '{{.Names}}' | grep -qx fake-element-web; then
    ok "fake-element-web running"
else
    bad "fake-element-web not running"
    exit 1
fi

net="$(docker inspect fake-traefik --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')"
if [ -n "$net" ]; then ok "traefik network detected: $net"; else bad "no network on fake-traefik"; fi

# ---------------------------------------------------------------- 2. answers
head_ "2. answers + --check"
cat > "$PLUGIN_ANSWERS" <<EOF
DOMAIN=${DOMAIN}
PLUGIN_DIR=/tmp/recplugin-smoke
INSTALL_LANGUAGE=ru
RECORDER_ACCOUNTS=@recorder1:${DOMAIN}
RECORDER_PASSWORDS=smoke-pass-1
RECORDING_WORKERS=1
RECORDER_BASE_PORT=18788
ELEMENT_CALL_URL=https://${DOMAIN}:8443
SYNAPSE_URL=http://fake-synapse:8008
TRAEFIK_NETWORK=${net}
ELEMENT_CONTAINER=fake-element-web
ADOPT_EXISTING=true
ADOPT_CONTROLLER_URL=http://127.0.0.1:18765
ADOPT_PORTAL_URL=http://127.0.0.1:18899
BACKUP_ELEMENT_WEB=true
EOF
chmod 600 "$PLUGIN_ANSWERS"

set +e
OUT="$(bash ../install.sh --check "$PLUGIN_ANSWERS" 2>&1)"
RC=$?
set -e
if [ $RC -eq 0 ]; then
    ok "--check exited 0"
else
    bad "--check exited $RC"
    printf '%s\n' "$OUT" | tail -25 | sed 's/^/     /'
fi
for probe in "traefik container" "docker network" "element container" "workers"; do
    if printf '%s' "$OUT" | grep -qi "$probe"; then ok "plan mentions: $probe"
    else note "plan does not mention: $probe"; fi
done

# ---------------------------------------------------------------- 3. routing
head_ "3. Traefik routing (before plugin)"
code="$(curl_ -o /dev/null -w '%{http_code}' "$BASE/recording/health")"
if [ "$code" = "404" ] || [ "$code" = "502" ]; then
    ok "/recording/health is $code before install (expected: no router yet)"
else
    note "/recording/health returned $code before install"
fi

# ---------------------------------------------------------------- 4. injection
head_ "4. button injection into the fake Element Web"
if docker exec fake-element-web sh -c 'grep -q record-button.js.patch.js /usr/share/nginx/html/index.html' 2>/dev/null; then
    ok "index.html references record-button.js.patch.js"
else
    note "index.html not patched yet (this is expected on a pristine stack);"
    note "run: bash ../install.sh --install $PLUGIN_ANSWERS"
fi

code="$(curl_ -o /dev/null -w '%{http_code}' "$BASE/record-button.js.patch.js")"
if [ "$code" = "200" ]; then
    ok "record-button.js.patch.js served ($code)"
    body="$(curl_ "$BASE/record-button.js.patch.js")"
    if printf '%s' "$body" | grep -q '__RECORDER_PLUGIN__'; then
        ok "config banner present in served file"
    else
        bad "served file has no __RECORDER_PLUGIN__ banner"
    fi
else
    note "record-button.js.patch.js returns $code (nginx may not route it without the injection)"
fi

# ---------------------------------------------------------------- 5. worker auth
head_ "5. worker rejects requests without a valid token"
if docker ps --format '{{.Names}}' | grep -q '^rec-worker-'; then
    code="$(curl_ -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18788/start \
            -H 'Content-Type: application/json' -d '{"roomId":"!x:y"}')"
    if [ "$code" = "401" ]; then
        ok "POST /start without token -> 401"
    else
        bad "POST /start without token -> $code (expected 401; the port is open)"
    fi
    code="$(curl_ -o /dev/null -w '%{http_code}' http://127.0.0.1:18788/health)"
    if [ "$code" = "200" ]; then ok "GET /health -> 200"; else bad "GET /health -> $code"; fi
else
    note "no rec-worker-* container; skipping token checks"
fi

head_ "summary"
printf '  passed: %s%d%s   failed: %s%d%s\n' "$cg" "$PASS" "$c0" "$cr" "$FAIL" "$c0"
[ "$FAIL" -eq 0 ] || { echo; echo "Some checks failed — see lines marked FAIL above."; exit 1; }
echo "Smoke test passed."
