# Matrix Element Call Recorder — плагин для Nginx (v3.1.0)

Кнопка записи E2EE-звонков для Matrix Element Call, устанавливаемая **на уже
работающий** сервер Matrix / Synapse / Element Web / Element Call / LiveKit.
Установщик не разворачивает Matrix и не трогает Synapse.

Запись идёт браузером: Puppeteer + Chromium заходят в комнату как обычный
участник, E2EE не обходится и не ослабляется, права комнаты не повышаются.

---

## Что нужно на сервере

- Ubuntu/Debian, Docker + compose v2, python3 ≥ 3.8
- Уже развёрнутый Element Web и Synapse в Docker
- Свободные порты: 18085 (UI/API плагина), 8765, 8899, 8788
- ОЗУ: ~1.5 ГБ на каждый worker плюс ~1 ГБ запаса
- Аккаунт бота-рекордера в Synapse (создаётся один раз, вручную)

---

## Установка

```bash
# 1. распаковать архив
tar -xzf plugin-recorder-nginx-3.1.0.tar.gz
cd plugin-recorder-nginx

# 2. подготовить ответы
cp answers.example /root/answers.env
chmod 600 /root/answers.env
nano /root/answers.env        # обычно достаточно DOMAIN="meet.example.com"

# 3. проверить, ничего не меняя
bash install.sh --check /root/answers.env

# 4. установить
bash install.sh --install /root/answers.env
```

`--check` ничего не изменяет и завершается кодом `2`, если есть блокеры.
Каждый блокер печатается вместе с готовой командой исправления.

После установки:

```bash
bash tools/verify.sh http://127.0.0.1:18085 /root/answers.env
```

---

## Два режима работы с nginx

**`NGINX_MODE="standalone"`** (по умолчанию) — плагин поднимает собственный
контейнер `record-plugin-static` на `127.0.0.1:18085` со своим каталогом
конфигов. Чужие конфиги не читаются и не изменяются. Это единственный режим,
который безопасно запускать вслепую.

**`NGINX_MODE="external"`** — плагин встраивается в nginx, который у вас уже
есть (контейнер или хостовый бинарь): кладёт фрагмент в `conf.d/includes/` и
вставляет одну строку `include` в `server{}`, который обслуживает ваш домен на
`:443`. Если подходящий `server{}` не единственный — установщик **отказывается**
править файл и печатает строку для ручной вставки.

Публикация наружу — на вашем фронт-прокси. Для режима `standalone` достаточно:

```nginx
location /recording/ {
    proxy_pass http://127.0.0.1:18085;
    proxy_set_header Host $host;
    proxy_buffering off;
}
location /recordings/ {
    proxy_pass http://127.0.0.1:18085;
    proxy_buffering off;
}
```

Traefik поддерживается так же: это обычный HTTP-апстрим на `127.0.0.1:18085`.
Установщик Traefik не настраивает и не требует.

---

## Команды

| Команда | Что делает |
|---|---|
| `--check` | Проверка окружения и конфликтов, план установки. Ничего не меняет. |
| `--install` | Установка или обновление. |
| `--repair` | Переприменить маршруты nginx, монтирования и UI-модуль без пересборки. |
| `--verify` | Сквозная проверка всех звеньев цепочки. |
| `--doctor` | Глубокая диагностика: где именно рвётся цепочка. |
| `--uninstall` | Удалить контейнеры и маршруты. Записи и профиль бота сохраняются. |

---

## Как устроена цепочка

```
record-button.js (ESM-модуль Element Web)
   POST /recording/api/record/start      Authorization: Bearer <matrix token>
        │
        ▼
    nginx (:18085)  ── /recording/api/ ──▶ rec-controller:8765
                                              │  проверяет токен в Synapse (whoami)
                                              │  проверяет power level в комнате
                                              ▼
                                        rec-worker (host network, :8788)
                                          Puppeteer + Chromium входят в комнату
                                          Xvfb :99 → ffmpeg x11grab + pulse → MP4
                                              │
                                              ▼
                                        rec-publisher → rec-portal:8899
                                        /recordings/ список записей
```

Проверка прав выполняется на сервере по токену вызывающего. Число `powerLevel`
из браузера не используется: его легко подделать.

---

## Требования к аккаунту бота

Аккаунт должен существовать в Synapse. Создать один раз:

```bash
docker exec -it <synapse-container> register_new_matrix_user \
    -c /data/homeserver.yaml -u spk --no-admin http://127.0.0.1:8008
```

Пароль от бота вписать в `answers.env` (`RECORDER_BOT_PASSWORDS`). Профиль с
E2EE-ключами бота живёт в `$PLUGIN_DIR/profile-spk` и **не удаляется** ни при
обновлении, ни при `--uninstall`.

---

## Диагностика

```bash
bash install.sh --doctor /root/answers.env
```

Печатает состояние каждого звена отдельно: контроллер напрямую, контроллер
через nginx, изнутри nginx-контейнера, worker, наличие общей docker-сети.
Типовые причины:

| Симптом | Причина | Что делать |
|---|---|---|
| `/recording/api/*` → 502 | контроллер не запущен | `docker logs rec-controller` |
| 502, контроллер жив | nginx и контроллер в разных сетях | `--repair` подключит сеть |
| 401 на start | истёк токен Matrix | перелогиниться в Element |
| 403 на start | power level < 50 | замерить права в комнате |
| 504 на api | контроллер не отвечает 120 с | смотреть `docker logs` |
| кнопки нет | UI-модуль не зарегистрирован | `--repair` |

---

## Безопасность

- Токен и пароли бота лежат в `answers.env` с режимом `600`; установщик
  отказывается работать с файлом, доступным другим пользователям.
- `rec-controller` и `rec-portal` публикуются только на `127.0.0.1`.
- Worker работает в `network_mode: host`, потому что ему нужен локальный
  Element Call; порт слушает только собственный HTTP-шим.
- Установщик не пишет в файлы, которые не создавал сам, и делает резервную
  копию перед любой правкой чужого конфига.
- `--uninstall` не удаляет записи и E2EE-профиль бота.

---

## Лицензия

См. `LICENSE`.
