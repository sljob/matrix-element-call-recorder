# Matrix Element Call Recorder (Инсталлятор)

Установщик «в одну команду», который разворачивает полноценный стек записи звонков Element Call для Matrix с ACL по комнатам и кнопкой записи, внедряемой в Element Web.

Важно: встроенный Jitsi отключается, используется только Element Call. Кнопка записи работает в Element Web; участники могут подключаться из Element Desktop и Element X (Android/iOS). Известная проблема на Element X Android: вместо видео может получиться «тёмная плитка» (см. https://github.com/element-hq/element-call/issues/3937).

## Что разворачивается (v0.1)

- Traefik reverse proxy (80/443/8448, TCP 5350 для TURN‑TLS)
- PostgreSQL (БД Synapse)
- Synapse (homeserver)
- Element Web (+ инъекция `record-button.js`)
- Element Call
- LiveKit с встроенным TURN
- JWT‑сервис для LiveKit
- Recorder (Chromium + Puppeteer + FFmpeg; host‑network)
- Recording controller (Flask API)
- Портал записей (Flask + gunicorn) с Matrix‑ACL
- Статика /.well-known для Matrix
- systemd unit `element-stack.service`

## Права доступа

- Запуск/останов записи — только админы комнаты (PL ≥ 50, настраивается `RECORDING_MIN_POWER_LEVEL`).
- Админ комнаты видит в портале только свои комнаты.
- Админ сервера видит все записи.

## Требования и ограничения

- OS: Ubuntu (ID=ubuntu), root
- Чистый Docker: до установки не должно быть контейнеров
- Свободные порты: 80, 443, 8448, 5350 (+ локальные)
- Сертификаты — локальные, подписанные вашим локальным CA; CA импортируется в NSS Chromium рекордера.
- Параллельные записи НЕ поддерживаются в v0.1 (одна активная запись).

## Порты по умолчанию

- Recorder API: 127.0.0.1:8788
- Element Call (локальная петля): 127.0.0.1:8090
- Записи (веб): 127.0.0.1:8899
- Synapse local: 127.0.0.1:18008
- LiveKit local: 127.0.0.1:17880
- LiveKit UDP: 7882, 3479, 30000–30020

## Каталоги (INSTALL_DIR=/opt/element-stack)

- certs/ {fullchain.pem, privkey.pem, ca.crt}
- config/ {element.json, call.json, traefik.json, dynamic.yml, element-index.html, record-button.js}
- data/{postgres,synapse,controller}/
- data/profile/ — постоянный профиль Matrix/крипто (НЕ удалять)
- out/ — черновики записей
- public/ — опубликованные записи и index
- src/{recorder,controller,recordings-auth}/
- well-known/.well-known/matrix/{client,server}
- .installer-config-sha256 — «отпечаток» answers.conf

## Health‑проверки (через traefik и локальный CA)

- curl --cacert certs/ca.crt --resolve DOMAIN:443:127.0.0.1 https://DOMAIN/_matrix/client/versions
- curl --cacert certs/ca.crt --resolve DOMAIN:443:127.0.0.1 https://DOMAIN/recording/health
- curl --cacert certs/ca.crt --resolve DOMAIN:443:127.0.0.1 https://DOMAIN/recordings/

## Установка

1) Положите в /root:
- `install.sh`
- `answers.conf` (или начните с `answers.example` и переименуйте в `answers.conf`)
- Локальные сертификаты CA и TLS:
  - TLS_CERT_FILE=/root/certs/fullchain.pem
  - TLS_KEY_FILE=/root/certs/privkey.pem
  - CA_CERT_FILE=/root/certs/ca.crt

2) Запуск:
```bash
chmod +x install.sh
sudo -E bash ./install.sh --install /root/answers.conf
```

3) Управление и логи:
```bash
systemctl enable --now element-stack.service
systemctl status element-stack.service
systemctl stop element-stack.service
docker compose -f /opt/element-stack/compose.json ps
docker compose -f /opt/element-stack/compose.json logs --tail=100
```

## answers.conf (обязательные ключи)

- DOMAIN, HOSTNAME, SERVER_IP, SYNAPSE_SERVER_NAME
- ADMIN_USER, ADMIN_PASSWORD
- RECORDER_USER, RECORDER_PASSWORD
- POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
- LIVEKIT_KEY, LIVEKIT_SECRET
- ELEMENT_WEB_IMAGE, ELEMENT_CALL_IMAGE, SYNAPSE_IMAGE
- POSTGRES_IMAGE, LIVEKIT_IMAGE, TRAEFIK_IMAGE
- JWT_IMAGE, ADMIN_IMAGE, NGINX_IMAGE
- TLS_CERT_FILE, TLS_KEY_FILE, CA_CERT_FILE

Опционально (по умолчанию):
- INSTALL_DIR (/opt/element-stack)
- RECORDER_API_PORT (8788), SOCAT_PORT (8090), RECORDINGS_WEB_PORT (8899)
- SYNAPSE_LOCAL_PORT (18008), LIVEKIT_LOCAL_PORT (17880)
- RECORDING_MIN_POWER_LEVEL (50)

## Известные ограничения

- Только одна параллельная запись (v0.1).
- Проблема «тёмной плитки» в Element X Android: https://github.com/element-hq/element-call/issues/3937

## Безопасность

- Никогда не коммитьте реальный `answers.conf` и приватные ключи.
- `data/profile/` содержит E2EE‑ключи рекордера — не удалять в проде.
- Инсталлятор требует `SYNAPSE_SERVER_NAME == DOMAIN` и использует локальный CA.
