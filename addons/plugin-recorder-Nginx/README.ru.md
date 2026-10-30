# Matrix Element Call Recorder — Nginx plugin 3.0.0-rc1

Самостоятельный плагин для **уже работающего** Matrix / Synapse / Element Web / Element Call / LiveKit с **Nginx на хосте**. Установщик не разворачивает заново Synapse, БД, TLS и LiveKit и не преобразует Traefik в Nginx.

**Статус: release candidate.** Локальные интеграционные проверки выполнены; реальный E2EE-звонок и установка Docker Compose на чистом сервере ещё не проверены. Не выдаётся за production-релиз. Эта сборка не должна автоматически заменять действующий тестовый стек с Traefik.

## Основа

Эталон: `sljob/matrix-element-call-recorder`, commit `4b2aa7eb421b90beaf917202bc05eea20f8a388b`. Исходный observer сохранён без изменений в `vendor/monolith/`; контрольные суммы — `vendor/PROVENANCE.json`.

Установщик создаёт адаптированную копию, меняя **только** путь Chromium, передачу SPKI/host-resolver и разрешённый локальный origin Element Call. Вход Matrix, device ID, сохранение браузерной криптобазы, join, ожидание медиа и FFmpeg взяты из эталона. Это означает и сохранение известных особенностей его ожидания видео; они не объявляются устранёнными. Механизм получения E2EE-ключей должен пройти живую приёмку.

Браузер — авторизованный участник звонка. E2EE не отключается, права комнаты не повышаются. `power=50` — стандартный порог для пользователя, управляющего записью/просмотром архива; сам бот может оставаться `power=0`, если RTC-события комнаты разрешают участие.

Новый интеграционный слой:

- `runtime/worker.mjs`: однослотовый worker, запуск эталонного observer, обработка stop, состояние на диске, финализация.
- `runtime/backend.py`: Matrix-авторизация, start/stop/status, SQLite, приватный архив, Range-запросы.
- `runtime/publish.py`: публикация только завершённого MP4, remux faststart, MP3/JPG, атомарный commit метаданных; исходник не удаляется.
- `ui/index.js`: Element Web Module API, адаптированный из Nginx-плагина, без DOM-инъекции и сборки npm.
- `installer/install.py`: проверка, установка, repair, проверка Nginx и автоматический откат файлов конфигурации при ошибке.

## Требования

Ubuntu/Debian Linux, Docker Engine, Compose v2, Python ≥3.11 на хосте, Nginx на хосте. Element Web должен поддерживать Module API. Проверялась форма Module API Element Web 1.12.x; другие версии требуют проверки.

Нужны **уже загруженные** образы:

1. Образ рекордера из монолита: Node ≥20, `/app/node_modules/puppeteer-core`, Chromium, FFmpeg/ffprobe, Python3, Xvfb, xdpyinfo, PulseAudio и pactl. Пример: `local/element-e2ee-recorder:v01`. Укажите реальный tag/digest. Установщик выполняет dependency probe через `docker run --rm --network none`; сборок и pull нет.
2. `python:3.11-slim-bookworm` либо имеющийся совместимый образ Python ≥3.11 для backend. Для воспроизводимости рекомендуется digest. Backend использует только стандартную библиотеку Python.

Nginx-конфигурация: один TLS `server {}` с точным `server_name` домена в явно указанном файле. Поддерживаются host Nginx, стандартный `listen … ssl`, обычный JSON Element config. Контейнерный Nginx, wildcard-only vhost, SSO-only bot без пароля и multi-worker в этой версии не поддержаны. Установщик отклоняет конфликтующие location, а не переписывает их.

Element config должен быть реальным файлом на хосте, из которого Element читает конфигурацию. Если образ генерирует runtime config отдельно и не читает этот файл, требуется корректно указать авторитетный host config и механизм его обновления. Автоматического поиска и редактирования временных файлов контейнера нет.

## Установка

Распакуйте проект в отдельный каталог, не поверх старого плагина. Скопируйте `answers.example.json` в `/root/recorder-nginx.answers.json`, заполните реальные значения и установите `chmod 600`.

Основные параметры:

| Параметр | Назначение |
|---|---|
| `domain` | Домен Element/Synapse, обслуживаемый TLS vhost Nginx |
| `synapse_url` | Доступный с хоста адрес Synapse, например `http://127.0.0.1:18008` |
| `element_call_url` | Прямой адрес Element Call с хоста, например `http://127.0.0.1:8090` |
| `livekit_internal` | Прямой адрес сигналинга LiveKit с хоста, `127.0.0.1:17880` без схемы |
| `recorder_image` | Существующий образ рекордера, содержащий зависимости монолита |
| `chromium_path` | Реальный executable внутри образа, не угадывается по имени |
| `bot_user`, `bot_password` | Полный MXID отдельного бота и его пароль |
| `device_id` | Постоянное устройство этого бота; не менять при repair |
| `profile_dir` | Постоянный каталог профиля; не удалять; fresh-профиль допустим |
| `element_config` | Авторитетный JSON config Element на хосте |
| `nginx_server_config` | Файл существующего TLS server block |
| `ca_file` | При частном CA — PEM-сертификат CA на хосте |
| `spki_hash` | При частном CA — SHA256 SPKI TLS leaf-сертификата для Chromium |
| `host_resolver` | При необходимости явные Chromium MAP rules; иначе пустая строка |
| `api_port`, `worker_port` | Свободные loopback-порты, по умолчанию 8765/8789 |

Профиль не обязан уже содержать `Default`. Для новой установки браузер создаёт его сам. Для миграции указывайте профиль **того же аккаунта и device ID**, предварительно остановив старый recorder. Не используйте один аккаунт/устройство одновременно в двух установках. Самостоятельного поиска, копирования или сброса существующих профилей нет.

Бот обычно создаётся заранее средствами Synapse. Для первоначального автоматического создания добавьте `bot_registration_secret_file`: путь к root-only файлу, содержащему `registration_shared_secret` Synapse. Только `--install` может создать **неадминистративный** аккаунт через shared-secret API. Существующие пароли не сбрасываются. Секреты не передаются в argv и не выводятся в журнал. Если аккаунт уже существует с другим паролем, установка остановится.

```bash
chmod +x install.sh tools/*.sh
./install.sh --check /root/recorder-nginx.answers.json
./install.sh --install /root/recorder-nginx.answers.json
```

`--check` проверяет существующий bot login через отдельное временное устройство, после чего выполняет logout только этого устройства. При намерении создать нового бота первый `--check` завершится ошибкой входа; создание выполняется `--install` при настроенном shared secret.

Установщик создаёт отдельный Compose project, запускает только два своих сервиса, добавляет Nginx include внутрь TLS server block и Module API entry в Element config. Nginx перегружается только после `nginx -t`. Все данные хранятся в `plugin_dir` и явно указанном `profile_dir`. Compose содержит пароль бота и закрыт режимом 0600; SQLite сессий и внутренний API token — в каталоге 0700.

Откройте Element заново/обновите конфигурацию. В приватной комнате пользователь, запускающий запись, должен иметь право приглашать бота; backend приглашает его пользовательским Matrix-токеном. Не предполагается, что порог power=50 автоматически даёт право invite: это зависит от комнаты. Отказ Synapse возвращается как ошибка, права не изменяются.

Архив: `https://<domain>/recordings/`. Вход по Matrix login/password. Доступ проверяется по текущему членству и power level **для каждого файла**. Глобальный администратор Synapse автоматически не получает доступ к чужим комнатам. Сессия архива использует Secure/HttpOnly/SameSite cookie; Matrix access token хранится в локальной SQLite, а не в cookie или URL. Истечение 8-часовой локальной сессии не гарантирует отзыв соответствующего Matrix-токена; используйте logout и политику срока действия сессий Synapse.

## Repair и откат

```bash
./install.sh --repair /root/recorder-nginx.answers.json
```

Сначала завершите запись. Repair отклоняет смену bot/device/profile. Каталоги записей и профиль сохраняются, токен worker переиспользуется. Каждая версия runtime хранится отдельно в `releases/`, резерв файлов — `backups/<timestamp>/`. `tools/rollback.py` восстанавливает конкретную резервную копию. Автоматический откат при неудачной установке восстанавливает конфигурацию, но не удаляет созданные записи или профиль и не откатывает устройство Matrix, уже создавшее ключи. Не откатывайте ответы/профиль от разных устройств как взаимозаменяемые.

В этой версии нет автоматического destructive uninstall. Для вывода из эксплуатации сохраните профиль/записи, остановите только этот Compose project, удалите помеченный Nginx include и Module API entry, выполните `nginx -t` и reload. Не применяйте `docker compose down -v` к основному Matrix-стеку.

## Проверки и границы результата

```bash
bash tools/verify.sh
python3 -m unittest discover -s tests -v
```

Тесты используют реальный FFmpeg и, при наличии, Nginx. Matrix API в тестах заменён локальным mock; браузерный observer в lifecycle-тесте — тестовый процесс. Поэтому зелёный результат **не доказывает** получение ключей настоящего E2EE-звонка и доступность вашего Docker registry/image. См. `VALIDATION.md` и `docs/ACCEPTANCE.ru.md`.

Для публикации через GitHub Desktop: `docs/GITHUB-DESKTOP.ru.md`. В релизном архиве нет answers реального сервера, профилей, CA тестового сервера, токенов, логов и приватных доменов.
