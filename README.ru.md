# Matrix Element Call Recorder — Универсальный инсталлятор

Статус: v0.1+ (обновлено 2026-10-08) • Ubuntu 22.04/24.04 • Docker/Compose

Инсталлятор разворачивает полный стек Matrix + Element с E2EE‑рекордером и порталом записей с ACL.

Что разворачивается
- Traefik (80/443/8448; TCP 5350 для TURN‑TLS)
- Postgres + Synapse
- Element Web и Element Call (встроенный Jitsi отключён)
- LiveKit (+ встроенный TURN)
- Рекордеры: Chromium + Puppeteer + FFmpeg (host‑network), по одному активному залу на воркер
- Контроллер (Flask) + публикация (MP4/mp3/постер, index.html)
- Портал записей (Flask+gunicorn) с Matrix‑ACL:
  - Серверные админы видят все записи
  - Админы комнат (PL ≥ 50) — только свои комнаты

Параллельные записи
- Один воркер пишет одну комнату.
- Параллельность настраивается переменной `RECORDING_WORKERS` (например, 4).
- Тестировалось 1–4 воркера. Планируйте ресурсы: ≈1 vCPU и 512–768 МБ RAM на 720p@15fps.

Ограничения
- Кнопка записи работает только в Element Web (не Desktop/X).
- Известная проблема Element X для Android: «тёмная плитка» при трансляции. Для записи используйте Element Web.
- `answers.conf` неизменяем после первого успешного запуска (фиксируется отпечаток).

Безопасность и TLS
- Используются локальные сертификаты локального УЦ. В эту версию добавлена корректная обработка смешанных DER/PEM‑цепочек; CA импортируется в NSS (Chromium доверяет).

Новое в этой версии
- Исправлена обработка смешанных DER/PEM‑сертификатов.
- Проверяется, что `SERVER_IP` действительно назначен серверу.
- Указанный IP используется в LiveKit `rtc.node_ip`.
- Защита от повторного запуска поверх старой/production‑установки.
- Минимальная установка зависимостей хоста.
- Включён поиск локальных Matrix‑пользователей по displayname.
- Проверка LiveKit discovery.
- Проверка соответствия медиа‑IP локальному адресу сервера.
- Проверка всех рекордеров, PulseAudio и Element Call.
- Дополнительные скрипты:
  - Проверка поиска по ФИО (displayname).
  - Read‑only полный скрипт верификации установки.

Быстрый старт
1) Скопируйте `install.sh` и создайте `/root/answers.conf` из `answers.example`.
2) Положите сертификаты:
   - `TLS_CERT_FILE=/root/certs/fullchain.pem`
   - `TLS_KEY_FILE=/root/certs/privkey.pem`
   - `CA_CERT_FILE=/root/certs/ca.crt`
3) Проверка без изменений:
   ```bash
   bash install.sh --check /root/answers.conf
   ```
4) Установка:
   ```bash
   bash install.sh --install /root/answers.conf
   systemctl enable --now element-stack.service
   ```

Проверки (через traefik и локальный CA)
```bash
cd /opt/element-stack
CACERT=certs/ca.crt
for p in /_matrix/client/versions /.well-known/matrix/client /recording/health /recordings/; do
  curl --silent --fail --cacert "$CACERT" --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN$p" >/dev/null && echo "OK $p" || echo "FAIL $p"
done
```

Основные маршруты
- https://DOMAIN/ → Element Web
- https://DOMAIN/_matrix, /_synapse → Synapse
- https://DOMAIN/call/ → Element Call
- https://DOMAIN/recording/* → Controller API
- https://DOMAIN/recordings/ → Портал записей
- https://DOMAIN/.well-known/matrix/* → Well‑known

Фрагмент answers.conf
```ini
DOMAIN=example.org
HOSTNAME=matrix
SERVER_IP=192.0.2.10
SYNAPSE_SERVER_NAME=example.org
ADMIN_USER=admin
ADMIN_PASSWORD=changeme
RECORDER_USER=recorder
RECORDER_PASSWORD=changeme
POSTGRES_USER=synapse
POSTGRES_PASSWORD=changeme
POSTGRES_DB=synapse
LIVEKIT_KEY=lk_key
LIVEKIT_SECRET=lk_secret

TLS_CERT_FILE=/root/certs/fullchain.pem
TLS_KEY_FILE=/root/certs/privkey.pem
CA_CERT_FILE=/root/certs/ca.crt

# Размер пула параллельных записей
RECORDING_WORKERS=4
```

Эксплуатация
```bash
systemctl status element-stack.service
docker compose -f /opt/element-stack/compose.json ps
docker compose -f /opt/element-stack/compose.json logs --tail=200 recorder
ls -lah /opt/element-stack/public
```

Типичные проблемы
- 403 при старте: у пользователя нет PL ≥ RECORDING_MIN_POWER_LEVEL.
- 401 к recorder API: проверьте связку `RECORDER_API_TOKEN` между пулом/контроллером и воркерами.
- «recorder is busy»: все воркеры заняты — увеличьте `RECORDING_WORKERS` или дождитесь освобождения.
- Synapse не стартует: права на data/synapse (uid/gid 991), наличие signing.key.
- Element X/Android: «тёмная плитка» — записывайте через Element Web.

Лицензия
- Apache‑2.0.
