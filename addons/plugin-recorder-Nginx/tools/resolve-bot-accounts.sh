#!/bin/bash
# ==========================================================================
#  tools/resolve-bot-accounts.sh — разобрать учётки ботов перед установкой
#
#  Зачем: если аккаунт бота уже существует, а пароля вы не знаете,
#  установка пройдёт, но первая запись упадёт на M_FORBIDDEN. Этот скрипт
#  показывает, что реально есть на сервере, и даёт три выхода:
#
#    --inspect          только посмотреть (по умолчанию, ничего не меняет)
#    --reset PASSWORD   задать известный пароль существующим аккаунтам
#    --delete           удалить аккаунты, чтобы установщик создал их заново
#    --set-env FILE     записать рабочие пароли в answers-файл
#
#  Как это делается без пароля: у Synapse есть admin API. Пароль меняется
#  через POST /_synapse/admin/v2/users/<user> с полем "password" — старый
#  пароль не нужен. Нужен только admin access token.
#
#  Токен берётся из:
#    1) переменной ADMIN_TOKEN
#    2) users/<admin>/access_tokens в БД Postgres (если доступна)
#    3) интерактивного логина администратора
#
#  Удаление аккаунта завершает его сессии и стирает E2EE-ключи бота.
#  Для профиля profile-spk это означает потерю истории устройства: бот
#  вступит заново как новый. Это нормально и не ломает записи, но знать
#  об этом надо заранее — скрипт спрашивает подтверждение.
# ==========================================================================
set -uo pipefail

USERS=""
PASSWORD=""
ACTION="inspect"
ENVFILE=""
SYNAPSE_URL="${SYNAPSE_URL:-http://127.0.0.1:18008}"
DOMAIN="${DOMAIN:-}"
ADMIN_TOKEN="${ADMIN_TOKEN:-}"
PG_CONTAINER="${PG_CONTAINER:-}"
SYN_CONTAINER="${SYN_CONTAINER:-}"
ASSUME_YES=0

R=$'\033[0m'; B=$'\033[1m'; C=$'\033[36m'; G=$'\033[32m'; Y=$'\033[33m'; E=$'\033[31m'
ok(){  printf '  %s[ OK ]%s %s\n' "$G" "$R" "$*"; }
wn(){  printf '  %s[WARN]%s %s\n' "$Y" "$R" "$*"; }
er(){  printf '  %s[FAIL]%s %s\n' "$E" "$R" "$*"; }
nf(){  printf '  %s[info]%s %s\n' "$C" "$R" "$*"; }
h(){   printf '\n%s%s%s\n' "$B" "$*" "$R"; }

usage(){
  cat <<'EOF'
Использование:
  bash tools/resolve-bot-accounts.sh --users bot1[,bot2] [действие] [опции]

Действия (по умолчанию --inspect):
  --inspect            показать состояние аккаунтов, ничего не менять
  --reset PASSWORD     задать пароль существующим аккаунтам (ADMIN_TOKEN нужен)
  --delete             удалить аккаунты, чтобы установщик создал заново
  --set-env FILE       записать рабочие пароли в answers-файл

Опции:
  --users LIST         список localpart через запятую: bot1,bot2
  --domain DOMAIN      домен сервера (иначе берётся из answers/DOMAIN)
  --synapse-url URL    по умолчанию http://127.0.0.1:18008
  --admin-token TOKEN  admin access token (иначе ищется в БД)
  --pg-container NAME  контейнер Postgres (иначе автоопределение)
  --syn-container NAME контейнер Synapse (иначе автоопределение)
  -y, --yes            не спрашивать подтверждение

Примеры:
  bash tools/resolve-bot-accounts.sh --users bot1 --inspect
  bash tools/resolve-bot-accounts.sh --users bot1 --reset 'NewPass123!' --set-env /root/answers.env
  bash tools/resolve-bot-accounts.sh --users bot1 --delete -y
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --users)        USERS="$2"; shift 2 ;;
    --users=*)      USERS="${1#*=}"; shift ;;
    --reset)        ACTION="reset"; PASSWORD="$2"; shift 2 ;;
    --reset=*)      ACTION="reset"; PASSWORD="${1#*=}"; shift ;;
    --delete)       ACTION="delete"; shift ;;
    --inspect)      ACTION="inspect"; shift ;;
    --set-env)      ENVFILE="$2"; shift 2 ;;
    --set-env=*)    ENVFILE="${1#*=}"; shift ;;
    --domain)       DOMAIN="$2"; shift 2 ;;
    --synapse-url)  SYNAPSE_URL="$2"; shift 2 ;;
    --admin-token)  ADMIN_TOKEN="$2"; shift 2 ;;
    --pg-container) PG_CONTAINER="$2"; shift 2 ;;
    --syn-container) SYN_CONTAINER="$2"; shift 2 ;;
    -y|--yes)       ASSUME_YES=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) er "неизвестный аргумент: $1"; usage; exit 2 ;;
  esac
done

if [ -z "$USERS" ]; then
  er "укажи --users bot1[,bot2]"
  usage; exit 2
fi

h "0. Окружение"
nf "synapse: $SYNAPSE_URL"

if [ -z "$PG_CONTAINER" ]; then
  PG_CONTAINER="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'postgres' | head -1)"
fi
if [ -z "$SYN_CONTAINER" ]; then
  SYN_CONTAINER="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'synapse' | grep -v jwt | head -1)"
fi
nf "postgres: ${PG_CONTAINER:-не найден}"
nf "synapse : ${SYN_CONTAINER:-не найден}"

if [ -z "$DOMAIN" ]; then
  DOMAIN="$(docker exec "$SYN_CONTAINER" sh -c \
    "grep -E '^server_name:' /data/homeserver.yaml 2>/dev/null | head -1 | awk '{print \$2}'" \
    2>/dev/null | tr -d '"'"'"' \r')"
fi
if [ -z "$DOMAIN" ]; then
  er "домен не определён, передай --domain"
  exit 1
fi
ok "домен: $DOMAIN"

psql_q(){
  docker exec -i "$PG_CONTAINER" psql -U synapse -d synapse -A -t -F $'\t' -c "$1" 2>/dev/null
}

h "1. Текущее состояние аккаунтов"
declare -a MISSING=() PRESENT=()
for local in ${USERS//,/ }; do
  uid="@${local}:${DOMAIN}"
  code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 \
    "$SYNAPSE_URL/_matrix/client/v3/profile/$(printf '%s' "$uid" | sed 's/@/%40/;s/:/%3A/')")"
  case "$code" in
    200) ok "$uid — существует"; PRESENT+=("$local") ;;
    404|403) wn "$uid — НЕ существует"; MISSING+=("$local") ;;
    *) wn "$uid — проверить не удалось (HTTP $code)" ;;
  esac
done

if [ -n "$PG_CONTAINER" ]; then
  h "2. Следы в БД (сессии, устройства, E2EE)"
  for local in "${PRESENT[@]:-}"; do
    [ -z "$local" ] && continue
    uid="@${local}:${DOMAIN}"
    ntok="$(psql_q "select count(*) from access_tokens where user_id='$uid';")"
    ndev="$(psql_q "select count(*) from devices where user_id='$uid';")"
    last="$(psql_q "select coalesce(max(last_seen),0) from devices where user_id='$uid';")"
    nf "$uid — токенов: ${ntok:-?}, устройств: ${ndev:-?}, последняя активность: ${last:-?}"
  done
fi

# --------------------------------------------------------------- admin token
need_admin(){
  [ -n "$ADMIN_TOKEN" ] && return 0
  h "3. Поиск admin access token"
  if [ -n "$PG_CONTAINER" ]; then
    ADMIN_TOKEN="$(psql_q "select token from access_tokens t
      join users u on u.name = t.user_id
      where u.admin = 1 and t.token is not null
      order by t.id desc limit 1;" | head -1)"
    if [ -n "$ADMIN_TOKEN" ]; then
      ok "токен администратора найден в БД (${#ADMIN_TOKEN} символов)"
      return 0
    fi
  fi
  er "admin access token не найден"
  cat <<'HINT'
     Задай его одним из способов:
       ADMIN_TOKEN='syt_...' bash tools/resolve-bot-accounts.sh ...
     Или возьми из Element: Настройки -> Помощь -> Дополнительно ->
     «Токен доступа» (нужна учётная запись администратора сервера).
HINT
  return 1
}

api(){
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sk -X "$method" --max-time 20 \
      -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
      -d "$body" "$SYNAPSE_URL$path" -w '\n%{http_code}'
  else
    curl -sk -X "$method" --max-time 20 \
      -H "Authorization: Bearer $ADMIN_TOKEN" \
      "$SYNAPSE_URL$path" -w '\n%{http_code}'
  fi
}

confirm(){
  [ "$ASSUME_YES" = "1" ] && return 0
  printf '  %sПродолжить? [y/N]: %s' "$Y" "$R"
  read -r ans
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

case "$ACTION" in

inspect)
  h "Итог осмотра"
  if [ "${#MISSING[@]}" -gt 0 ]; then
    wn "отсутствуют: ${MISSING[*]} — установщик создаст их, если задан пароль"
  fi
  if [ "${#PRESENT[@]}" -gt 0 ]; then
    if [ -z "${RECORDER_BOT_PASSWORDS:-}" ]; then
      wn "существуют: ${PRESENT[*]} — пароль неизвестен"
      nf "если пароль не совпадает с answers, запись упадёт на M_FORBIDDEN"
      nf "реши заранее:  --reset <пароль>   либо   --delete"
    else
      ok "существуют: ${PRESENT[*]}"
    fi
  fi
  ;;

reset)
  [ -n "$PASSWORD" ] || { er "--reset требует пароль"; exit 2; }
  need_admin || exit 1
  h "Сброс пароля (${#PRESENT[@]} аккаунт(ов))"
  wn "смена пароля завершает все текущие сессии бота — это ожидаемо"
  confirm || { nf "отменено"; exit 0; }
  okc=0
  for local in "${PRESENT[@]:-}"; do
    [ -z "$local" ] && continue
    uid="@${local}:${DOMAIN}"
    out="$(api PUT "/_synapse/admin/v2/users/$uid" \
      "{\"password\":$(printf '%s' "$PASSWORD" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')}")"
    code="$(printf '%s' "$out" | tail -1)"
    if [ "$code" = "200" ]; then
      ok "$uid — пароль обновлён"; okc=$((okc+1))
    else
      er "$uid — HTTP $code: $(printf '%s' "$out" | head -c 200)"
    fi
  done

  h "Проверка логином"
  for local in "${PRESENT[@]:-}"; do
    [ -z "$local" ] && continue
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 \
      -H 'Content-Type: application/json' \
      -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"$local\"},\"password\":$(printf '%s' "$PASSWORD" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')}" \
      "$SYNAPSE_URL/_matrix/client/v3/login")"
    [ "$code" = "200" ] && ok "$local — логин проходит" || er "$local — логин HTTP $code"
  done

  if [ -n "$ENVFILE" ]; then
    h "Запись в $ENVFILE"
    [ -f "$ENVFILE" ] || { er "файла нет: $ENVFILE"; exit 1; }
    cp -a "$ENVFILE" "$ENVFILE.bak"
    python3 - "$ENVFILE" "$PASSWORD" <<'PY'
import re, sys
path, pw = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
new, n = re.subn(r'^RECORDER_BOT_PASSWORDS=.*$',
                 'RECORDER_BOT_PASSWORDS="%s"' % pw, text, flags=re.M)
if n == 0:
    new = text.rstrip("\n") + '\nRECORDER_BOT_PASSWORDS="%s"\n' % pw
open(path, "w", encoding="utf-8").write(new)
print("  обновлено вхождений:", n or 1)
PY
    chmod 600 "$ENVFILE"
    ok "пароль записан, бэкап: $ENVFILE.bak"
  fi
  ;;

delete)
  need_admin || exit 1
  h "Удаление аккаунтов"
  wn "удаление стирает устройство и E2EE-ключи бота."
  wn "профиль profile-spk останется на диске, но бот вступит как новый."
  wn "файлы записей не затрагиваются."
  confirm || { nf "отменено"; exit 0; }
  for local in "${PRESENT[@]:-}"; do
    [ -z "$local" ] && continue
    uid="@${local}:${DOMAIN}"
    # deactivate=1 освобождает localpart, чтобы регистрация прошла заново
    out="$(api POST "/_synapse/admin/v1/deactivate/$uid" '{"erase":false}')"
    code="$(printf '%s' "$out" | tail -1)"
    [ "$code" = "200" ] && ok "$uid — деактивирован" || er "$uid — HTTP $code: $(printf '%s' "$out" | head -c 160)"
  done
  h "Дальше"
  nf "запусти установку: он создаст аккаунты заново с паролем из answers"
  nf "profиль E2EE при необходимости почисти: rm -rf <PLUGIN_DIR>/profile/<bot>"
  ;;
esac

h "Готово"
nf "проверка перед установкой: bash install.sh --check <answers>"
