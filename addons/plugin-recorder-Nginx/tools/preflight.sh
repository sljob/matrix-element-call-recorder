#!/bin/bash
# =============================================================================
#  plugin-recorder 2.0.1 — preflight, read-only.
#  Проверяет готовность ДО установки: файлы, бинарники, answers, код-гарды,
#  синтаксис, порты и РЕАЛЬНЫЙ парсинг nginx.example.conf через nginx -t.
#  Ничего не ставит, не перезапускает. Код возврата: 0 = блокеров нет.
#
#  bash /root/03_preflight.sh [PLUGIN_DIR] [ANSWERS]
# =============================================================================
set -uo pipefail

P="${1:-}"; A="${2:-}"
F=0; W=0; declare -a FL=()
if [ -t 1 ]; then R=$'\033[0m'; G=$'\033[32m'; Y=$'\033[33m'; E=$'\033[31m'; B=$'\033[1m'; C=$'\033[36m'
else R=''; G=''; Y=''; E=''; B=''; C=''; fi
ok(){  printf '  %s[ OK ]%s %s\n' "$G" "$R" "$*"; }
wn(){  printf '  %s[WARN]%s %s\n' "$Y" "$R" "$*"; W=$((W+1)); }
fl(){  printf '  %s[FAIL]%s %s\n' "$E" "$R" "$*"; F=$((F+1)); FL+=("$*"); }
nfo(){ printf '  %s[info]%s %s\n' "$C" "$R" "$*"; }
h(){   printf '\n%s%s%s\n' "$B" "$*" "$R"; }

val(){ grep -E "^[[:space:]]*$1[[:space:]]*=" "$2" 2>/dev/null | tail -1 \
         | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
         -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" -e 's/\r$//'; }

# --- 0. дерево плагина -------------------------------------------------------
h "0. Дерево плагина"
if [ -z "$P" ]; then
  for c in /root/plugin-recorder /root/plugin-recorder-2.0.1 /matrix/recorder-plugin \
           /opt/recorder-plugin "$PWD" "$PWD/addons/plugin-recorder"; do
    [ -f "$c/install.sh" ] && [ -f "$c/installer/install.py" ] && { P="$c"; break; }
  done
fi
[ -d "${P:-/nonexistent}" ] || { fl "каталог плагина не найден: bash $0 /path/to/plugin-recorder"; exit 1; }
P="$(cd "$P" && pwd)"; ok "plugin dir: $P"

miss=0
for f in install.sh installer/install.py nginx.example.conf answers.example manifest.json \
         index.js package.json src/controller/app.py src/portal/index.py \
         src/recorder/server.js src/recorder/Dockerfile src/recorder/entrypoint.sh \
         src/publisher/publish.py locales/ru.json locales/en.json; do
  [ -s "$P/$f" ] || { fl "нет или пуст: $f"; miss=1; }
done
[ "$miss" -eq 0 ] && ok "все обязательные файлы на месте"

if [ -s "$P/vendor/p63-observer.js" ]; then
  ok "vendor/p63-observer.js ($(stat -c%s "$P/vendor/p63-observer.js") б)"
elif [ -s "$P/src/recorder/p63-observer.js" ]; then
  wn "vendor пуст, но src/recorder/p63-observer.js есть — запусти: bash tools/fetch-upstream.sh"
else
  fl "нет ядра рекордера: ни vendor/p63-observer.js, ни src/recorder/p63-observer.js"
  nfo "src/recorder/Dockerfile делает COPY p63-observer.js — без файла сборка воркера упадёт"
  nfo "лечение: положить p63-observer.js в src/recorder/, затем bash tools/fetch-upstream.sh"
fi

# --- 1. компоненты хоста -----------------------------------------------------
h "1. Компоненты хоста"
for pair in "docker:docker.io:Docker Engine" "curl:curl:curl" "jq:jq:jq" "openssl:openssl:openssl"; do
  b="${pair%%:*}"; r="${pair#*:}"; pkg="${r%%:*}"; l="${r#*:}"
  if command -v "$b" >/dev/null 2>&1; then ok "$l -> $(command -v "$b")"
  else fl "$l отсутствует — apt-get install -y $pkg"; fi
done
command -v python3 >/dev/null 2>&1 && ok "python3 -> $(command -v python3)" \
  || fl "python3 отсутствует (нужен installer/install.py)"
if docker compose version >/dev/null 2>&1; then
  ok "docker compose v2: $(docker compose version --short 2>/dev/null)"
else fl "docker compose v2 отсутствует — apt-get install -y docker-compose-v2"; fi
docker info >/dev/null 2>&1 && ok "docker daemon доступен" || fl "docker daemon недоступен"
if command -v ffmpeg >/dev/null 2>&1; then
  nfo "ffmpeg на хосте есть — в overlay-режиме не требуется"
else nfo "ffmpeg на хосте нет — норма: рендеринг в контейнере воркера"; fi

# --- 2. nginx и его конфиг ---------------------------------------------------
h "2. Nginx"
NB=""
if command -v nginx >/dev/null 2>&1; then
  NB="$(command -v nginx)"; ok "nginx $(nginx -v 2>&1 | sed 's/^nginx version: //')"
else wn "nginx не в PATH — если прокси в контейнере, этот блок выполнять там"; fi

CONF="$P/nginx.example.conf"
if [ -s "$CONF" ]; then
  ok "nginx.example.conf есть"
  if grep -qE 'proxy_pass[[:space:]]+https?://[0-9]+\.[0-9]+(\.[0-9]+)?[[:space:]]*;' "$CONF"; then
    fl "proxy_pass без порта — nginx не запустится (host not found in upstream)"
  else ok "нет proxy_pass без порта"; fi

  bs=0
  while IFS= read -r ln; do
    case "$ln" in *'/'*) ;; *) bs=1 ;; esac
  done < <(grep -E '^[[:space:]]*proxy_pass[[:space:]]+http://(127\.0\.0\.1|rec-controller|rec-portal)' "$CONF")
  if [ "$bs" -eq 0 ]; then ok "все proxy_pass со слэшем на конце (префикс стрипается)"
  else fl "proxy_pass без '/' — префикс /recording не стрипнется, будет 404"; fi

  grep -q 'location /files/' "$CONF" && ok "location /files/ присутствует" \
    || wn "нет location /files/ — ссылки портала могут не открыться"
  if grep -vE '^[[:space:]]*#' "$CONF" | grep -q 'proxy_read_timeout'; then
    wn "в примере активен proxy_read_timeout — не нужен, /stop отдаёт 202 сразу"
  else ok "нет лишнего proxy_read_timeout (верно: stop асинхронный)"; fi
else fl "nginx.example.conf отсутствует"; fi

# --- 3. answers --------------------------------------------------------------
h "3. Answers"
if [ -z "$A" ]; then
  for c in /root/answers.env /root/answers-plugin.conf "$P/answers.env"; do
    [ -s "$c" ] && { A="$c"; break; }
  done
fi
if [ ! -s "${A:-/nonexistent}" ]; then
  wn "answers не найден — проверки ключей пропущены (передай 2-м аргументом)"
else
  ok "answers: $A"
  case "$(stat -c%a "$A")" in
    600|400) ok "права $(stat -c%a "$A")" ;;
    *) wn "права $(stat -c%a "$A") — внутри секреты, нужно chmod 600" ;;
  esac
  for k in DOMAIN RECORDING_MIN_POWER_LEVEL RECORDINGS_MIN_POWER_LEVEL PUBLISH_LOOPBACK_PORTS; do
    v="$(val "$k" "$A")"
    if [ -n "$v" ]; then ok "$k=$v"
    else
      case "$k" in
        RECORDINGS_MIN_POWER_LEVEL) wn "$k нет — портал возьмёт 50 (порог просмотра)" ;;
        PUBLISH_LOOPBACK_PORTS)     wn "$k нет — по умолчанию true, для nginx это и нужно" ;;
        *) fl "$k отсутствует и безопасного умолчания нет" ;;
      esac
    fi
  done
  if [ "$(val PUBLISH_LOOPBACK_PORTS "$A")" = "false" ] && [ -n "$NB" ]; then
    fl "PUBLISH_LOOPBACK_PORTS=false, а nginx на хосте: 127.0.0.1:8765 не ответит"
  fi
  [ "$(val ADOPT_EXISTING "$A")" = "true" ] \
    && nfo "ADOPT_EXISTING=true — overlay не собирается, nginx смотрит в существующий стек"
fi

# --- 4. гарды в исходниках ---------------------------------------------------
h "4. Гарды в коде"
if grep -q 'RECORDINGS_MIN_POWER_LEVEL' "$P/src/portal/index.py" 2>/dev/null; then
  ok "портал читает RECORDINGS_MIN_POWER_LEVEL"
  n=$(grep -c 'VIEW_MIN' "$P/src/portal/index.py")
  if [ "$n" -ge 3 ]; then ok "VIEW_MIN в $n местах (список + скачивание)"
  else wn "VIEW_MIN только $n раз — проверь allowed_entries() и file()"; fi
else fl "в портале нет RECORDINGS_MIN_POWER_LEVEL — участники увидят все записи"; fi

grep -q 'PUBLISH_LOOPBACK_PORTS' "$P/installer/install.py" 2>/dev/null \
  && ok "installer учитывает PUBLISH_LOOPBACK_PORTS" \
  || fl "installer игнорирует PUBLISH_LOOPBACK_PORTS — порты не опубликуются"

if grep -q 'RECORDING_MIN_POWER_LEVEL' "$P/installer/install.py" 2>/dev/null \
   && grep -q 'RECORDING_MIN_POWER_LEVEL' "$P/src/controller/app.py" 2>/dev/null; then
  ok "RECORDING_MIN_POWER_LEVEL протянут: answers -> compose -> controller"
else fl "RECORDING_MIN_POWER_LEVEL не протянут end-to-end"; fi

# --- 5. синтаксис ------------------------------------------------------------
h "5. Синтаксис"
for s in install.sh tools/fetch-upstream.sh testbed/smoke.sh; do
  [ -f "$P/$s" ] || continue
  if bash -n "$P/$s" 2>/dev/null; then ok "bash -n $s"; else fl "bash -n $s"; fi
done
if command -v python3 >/dev/null 2>&1; then
  for p in installer/install.py src/controller/app.py src/portal/index.py \
           src/publisher/publish.py src/adopt/proxy.py; do
    [ -f "$P/$p" ] || continue
    if python3 -m py_compile "$P/$p" 2>/dev/null; then ok "py_compile $p"; else fl "py_compile $p"; fi
  done
  find "$P" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
fi
if command -v node >/dev/null 2>&1; then
  for j in "$P"/*.js "$P"/src/recorder/*.js; do
    [ -f "$j" ] || continue
    if node --check "$j" >/dev/null 2>&1; then ok "node --check ${j#$P/}"
    else fl "node --check ${j#$P/}"; fi
  done
else wn "node нет — JS не проверен (поймает сборка образа)"; fi
for j in manifest.json package.json locales/ru.json locales/en.json; do
  [ -f "$P/$j" ] || continue
  if python3 -c 'import json,sys;json.load(open(sys.argv[1]))' "$P/$j" 2>/dev/null; then
    ok "JSON валиден: $j"
  else fl "JSON битый: $j"; fi
done

# --- 6. порты бэкендов -------------------------------------------------------
h "6. Порты бэкендов"
own(){ command -v ss >/dev/null 2>&1 && ss -lntp 2>/dev/null | awk -v P=":$1" '$4 ~ P"$"{print $4"  "$6;exit}'; }
for pt in 8765 8899; do
  o="$(own "$pt")"
  if [ -n "$o" ]; then ok "порт $pt слушается — $o"
  elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qE '^rec-(controller|portal)$'; then
    fl "порт $pt не слушается, но контейнер rec-* есть — смотри docker logs ре-контейнера"
  else wn "порт $pt не слушается — норма ДО установки, блокер ПОСЛЕ"; fi
done
if command -v docker >/dev/null 2>&1; then
  for c in rec-controller rec-portal rec-publisher; do
    docker inspect "$c" >/dev/null 2>&1 || continue
    st="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null)"
    if [ "$st" = "running" ]; then ok "$c running"; else fl "$c state=$st"; fi
  done
  n=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -c '^rec-worker-' || true)
  if [ "${n:-0}" -gt 0 ]; then ok "воркеров запущено: $n"; else nfo "rec-worker-* пока нет"; fi
fi

# --- 7. настоящий парсинг конфига -------------------------------------------
h "7. nginx -t на примере"
if [ -n "$NB" ] && [ -s "$CONF" ]; then
  T="$(mktemp -d /tmp/rpfl.XXXXXX)"
  trap 'rm -rf "$T"' EXIT
  mkdir -p "$T/logs" "$T/body" "$T/proxy"
  {
    echo 'worker_processes 1;'
    echo "error_log $T/logs/error.log;"
    echo "pid $T/nginx.pid;"
    echo 'events { worker_connections 64; }'
    echo 'http {'
    echo '  default_type application/octet-stream;'
    echo '  access_log off;'
    echo "  client_body_temp_path $T/body;"
    echo "  proxy_temp_path $T/proxy;"
    echo "  fastcgi_temp_path $T/proxy;"
    echo "  uwsgi_temp_path $T/proxy;"
    echo "  scgi_temp_path $T/proxy;"
    echo '  server { listen 127.0.0.1:18089; server_name _;'
    sed 's/^[[:space:]]*//' "$CONF" | grep -v '^#' | grep -v '^$'
    echo '  }'
    echo '}'
  } > "$T/n.conf" 2>/dev/null
  if out="$("$NB" -t -c "$T/n.conf" -p "$T" 2>&1)"; then
    ok "nginx -t прошёл — блоки locations парсятся, имена резолвятся"
  else
    if printf '%s' "$out" | grep -q 'host not found in upstream'; then
      fl "nginx -t: неизвестный upstream — в proxy_pass нет валидного хоста/порта"
    else
      fl "nginx -t упал: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
    fi
    if cp "$T/n.conf" /tmp/recorder-preflight.nginx.conf 2>/dev/null; then
      nfo "копия конфига для разбора: /tmp/recorder-preflight.nginx.conf"
    fi
  fi
else
  wn "нет nginx или nginx.example.conf — реальный парсинг не выполнен"
fi

# --- 8. живой конфиг nginx ---------------------------------------------------
h "8. Живой конфиг nginx"
if [ -n "$NB" ]; then
  if "$NB" -t >/dev/null 2>&1; then
    ok "текущий живой конфиг проходит nginx -t"
  else
    fl "текущий живой конфиг НЕ проходит nginx -t — починить до добавления блоков"
    "$NB" -t 2>&1 | tail -3 | sed 's/^/          /'
  fi
  S=""
  for d in /etc/nginx/sites-enabled /etc/nginx/conf.d; do [ -d "$d" ] && S="$S $d"; done
  if [ -n "$S" ]; then
    hit="$(grep -rl '/recording' $S 2>/dev/null || true)"
    if [ -n "$hit" ]; then
      ok "блок /recording уже есть: $hit"
      grep -rn 'proxy_pass' $hit 2>/dev/null | grep -E '8765|8899' | sed 's/^/          /'
    else
      nfo "блоков /recording и /recordings ещё нет — locations надо добавить"
    fi
  fi
fi

# --- 9. что делать после установки ------------------------------------------
h "9. Проверка работы (вручную, по порядку)"
cat <<'EOT'
  1) Сервисы живы:
       docker compose -p recording-overlay ps
       curl -s http://127.0.0.1:8765/health | jq .
       curl -s http://127.0.0.1:8899/healthz | jq .

  2) Прокси доходит до контроллера (ждём 200/401, НЕ 404):
       curl -s -o /dev/null -w '%{http_code}\n' https://<domain>/recording/health
     200/401 = маршрут + стрип + бэкенд в порядке
     404      = префикс /recording не стрипнут -> нет слэша в proxy_pass

  3) Архив закрыт для анонима (ждём не-200):
       curl -s -o /dev/null -w '%{http_code}\n' https://<domain>/recordings/

  4) Кнопка реально доставлена в браузер:
       curl -s https://<domain>/config.json | jq '.modules'
       curl -s -o /dev/null -w '%{http_code}\n' https://<domain>/modules/recorder/index.js

  5) Живая запись, затем:
       docker logs --tail 50 rec-controller
       docker logs --tail 50 rec-worker-1
       ls -lt /opt/recorder-plugin/public | head

  6) Статус после остановки:
       curl -s -H "Authorization: Bearer $TOKEN" \
         "https://<domain>/recording/api/record/status?roomId=!room:example.org" | jq .state
     recording -> stopping -> processing -> published

  Порог просмотра (новое в 2.0.1):
       RECORDINGS_MIN_POWER_LEVEL="50"   # PL>=50 видит записи комнаты
EOT

h "Итог"
if [ "$F" -eq 0 ]; then
  printf '  %sБлокеров нет.%s  предупреждений: %d\n  Дальше: bash install.sh --check %s\n' \
    "$G" "$R" "$W" "${A:-<answers.env>}"
  exit 0
else
  printf '  %sБлокеров: %d%s\n' "$E" "$F" "$R"
  for x in "${FL[@]}"; do printf '    - %s\n' "$x"; done
  printf '  предупреждений: %d\n' "$W"
  exit 1
fi
