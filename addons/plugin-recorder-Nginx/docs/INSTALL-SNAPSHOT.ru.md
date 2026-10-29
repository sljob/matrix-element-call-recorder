# Установка на чистый снепшот

Полная последовательность: от передачи файлов до проверки записи. Ничего не
предполагается о состоянии сервера, кроме того, что на нём уже работает
Matrix-стек с Element Web за nginx.

## Что нужно до начала

| Компонент | Требование |
|---|---|
| ОС | Ubuntu 22.04 / 24.04 (Debian 12 подходит) |
| Docker | Engine 24+ и плагин `docker compose` v2 |
| Synapse | работающий, с доступом к `/_matrix/client/v3/login` изнутри хоста |
| Element Web | контейнер, отдающий `config.json` |
| nginx | бинарь на хосте **или** контейнер, обслуживающий ваш домен |
| RAM | ~2 ГБ на воркер записи + 2 ГБ на стек |
| Диск | 5 ГБ минимум, записи растут быстро |
| Аккаунты | бот-рекордер: либо готовый, либо `registration_shared_secret` для создания |

## Шаг 1. Передать файлы на сервер

Архив содержит каталог `plugin-recorder-Nginx/`. Разворачивать его нужно в
путь, который не пересекается с существующим стеком.

### Вариант А: с локальной машины (W11) через scp

Скачайте `plugin-recorder-Nginx.zip` из выдачи, распакуйте, затем:

```powershell
# в PowerShell на W11, из папки, где лежит распакованный архив
scp -r .\plugin-recorder-Nginx root@ВАШ_СЕРВЕР:/root/
```

### Вариант Б: прямо на сервере из GitHub

**Сначала ветка должна существовать на GitHub.** Если её там нет, клон падает:

```
fatal: Remote branch plugin-recorder-Nginx not found in upstream origin
```

Это не ошибка команды: ветку надо запушить. Порядок — в разделе
[«Публикация ветки»](#публикация-ветки) ниже. После публикации:

```bash
cd /root
git clone --depth 1 --branch plugin-recorder-Nginx \
    https://github.com/sljob/matrix-element-call-recorder.git plugin-recorder-Nginx
```

Проверить, что ветка уже на GitHub:

```bash
git ls-remote --heads https://github.com/sljob/matrix-element-call-recorder.git | grep nginx
```

Пустой вывод означает, что ветки ещё нет.

### Вариант В: архив уже на сервере

```bash
cd /root
unzip -q plugin-recorder-Nginx.zip
# или
tar xzf plugin-recorder-Nginx.tar.gz
```

Проверка, что доехало целиком:

```bash
cd /root/plugin-recorder-Nginx
find . -type f | wc -l          # ожидается 42-44
ls vendor/p63-observer.js       # ядро записи должно быть на месте
```

Если `vendor/p63-observer.js` отсутствует или пуст — ядро не доехало:

```bash
bash tools/fetch-upstream.sh
```

## Публикация ветки

Ветка `plugin-recorder-Nginx` в репозитории не создана — отсюда и
`fatal: Remote branch not found`. Плагин добавляется не в `main`, а в
отдельную ветку.

### Через GitHub Desktop на W11

1. Скачайте `plugin-recorder-Nginx.zip` и распакуйте в удобное место.
2. GitHub Desktop → **File → Clone repository** → вкладка **GitHub.com** →
   `sljob/matrix-element-call-recorder` → Clone. Получите локальную копию в
   `C:\Users\<вы>\Documents\GitHub\matrix-element-call-recorder`.
3. Ветка: **Current Branch → New Branch** → имя `plugin-recorder-Nginx` →
   **Create branch**.
4. Скопируйте распакованный каталог `plugin-recorder-Nginx` **внутрь** `addons/`:

   ```powershell
   # PowerShell, подставьте свои пути
   Copy-Item -Recurse -Force `
       "$HOME\Downloads\plugin-recorder-Nginx" `
       "$HOME\Documents\GitHub\matrix-element-call-recorder\addons\plugin-recorder-Nginx"
   ```

   Итоговый путь должен быть:
   `addons/plugin-recorder-Nginx/install.sh`

5. GitHub Desktop покажет несколько десятков новых файлов. Проверьте, что
   **не попали** `__pycache__`, `*.pyc` и `answers.env` с паролями.
6. Summary: `addons/plugin-recorder-Nginx: nginx edition with pre-install checks`.
7. **Commit to plugin-recorder-Nginx**, затем **Publish branch**.

### Через git в командной строке

```bash
cd /path/to/matrix-element-call-recorder
git checkout -b plugin-recorder-Nginx
cp -r /path/to/plugin-recorder-Nginx addons/
git add addons/plugin-recorder-Nginx
git status --short | grep -E '__pycache__|\.pyc|answers\.env' && echo "УБЕРИ ЭТО"
git commit -m "addons/plugin-recorder-Nginx: nginx edition with pre-install checks"
git push -u origin plugin-recorder-Nginx
```

### Что не должно попасть в коммит

| Файл | Почему |
|---|---|
| `answers.env`, `answers-plugin-nginx.conf` с реальными паролями | секреты |
| `__pycache__/`, `*.pyc` | мусор сборки |
| `vendor/p63-observer.js`, если он тянется из `main` | дубль; но для самодостаточной ветки он **нужен** |
| `profile-spk/`, `public/`, `output/` | рабочие данные |

`vendor/p63-observer.js` в этой ветке включён намеренно: без него установка
непройдёт проверку `OBS` и не соберётся образ воркера.

### После публикации

```bash
git ls-remote --heads https://github.com/sljob/matrix-element-call-recorder.git | grep nginx
# ожидается строка с refs/heads/plugin-recorder-Nginx
```

Теперь вариант Б на шаге 1 работает.

## Шаг 2. Создать answers

```bash
cd /root/plugin-recorder-Nginx
cp answers.example /root/answers.env
chmod 600 /root/answers.env
```

Минимум, что нужно вписать руками:

```bash
DOMAIN="matrix.example.org"          # ваш домен, без https://
BASE_DIR="auto"                      # автоопределение
ELEMENT_WEB_CONTAINER="auto"
NGINX_CONTAINER="auto"               # или имя контейнера, если nginx в докере
RECORDER_BOT_COUNT="1"
RECORDER_BOT_USERS="recbot1"         # либо оставить пустым и задать PREFIX
RECORDER_BOT_PASSWORDS="НАДЁЖНЫЙ_ПАРОЛЬ"
```

Пароль бота — единственное поле, ошибка в котором даёт падение на первой
записи. Либо задайте его здесь, либо разберитесь с существующим аккаунтом на
шаге 3.

## Шаг 3. Разобраться с учёткой бота **до** установки

Это тот шаг, которого раньше не было и из-за которого установка проходила, а
запись не работала.

```bash
cd /root/plugin-recorder-Nginx
bash tools/resolve-bot-accounts.sh --users recbot1 --inspect
```

Вывод скажет одно из трёх:

**Аккаунта нет** — всё в порядке, установщик создаст его с паролем из answers.
Нужен `registration_shared_secret` в `homeserver.yaml`.

**Аккаунт есть, пароль известен** — впишите тот же пароль в
`RECORDER_BOT_PASSWORDS`, и проверка на шаге 4 пройдёт.

**Аккаунт есть, пароль неизвестен** — выберите один из двух путей:

```bash
# предпочтительно: задать пароль, сохранив устройство и E2EE-сессию бота
bash tools/resolve-bot-accounts.sh --users recbot1 \
     --reset 'НовыйПароль123!' --set-env /root/answers.env

# либо удалить и дать создать заново (бот станет новым устройством)
bash tools/resolve-bot-accounts.sh --users recbot1 --delete
```

`--reset` требует admin access token. Скрипт берёт его из таблицы
`access_tokens` (учётка с `admin = 1`). Если не нашёл — передайте явно:

```bash
ADMIN_TOKEN='syt_...' bash tools/resolve-bot-accounts.sh --users recbot1 --reset '...'
```

Токен можно взять в Element: Настройки → Помощь и о программе → Дополнительно →
Токен доступа (под администратором).

## Шаг 4. Проверка перед установкой

```bash
cd /root/plugin-recorder-Nginx
bash install.sh --check /root/answers.env
```

Читайте отчёт сверху вниз: сначала блокеры. Код возврата `2` означает, что
установка не пойдёт, пока блокеры не устранены. Ничего на сервере при этом не
изменено.

Типичные блокеры и что с ними делать:

| Блокер | Действие |
|---|---|
| `BOT ... пароль НЕ подходит` | шаг 3, `--reset` или `--delete` |
| `PORT ... занят процессом` | `ss -lntp \| grep :8765`, освободить или сменить `CONTROLLER_PORT` |
| `PORT ... занят контейнером` | остановить чужой контейнер либо сменить порт |
| `NGX другой конфиг уже обслуживает /recording/` | убрать старый блок из чужого vhost |
| `NGX nginx -t падает` | починить nginx **до** установки плагина |
| `OBS в observer нет фиксов` | `bash tools/fetch-upstream.sh` |
| `NET сети плагина нет` | `docker network create <имя>` |
| `ANS дубли ключей` | убрать повтор в `answers.env` |

## Шаг 5. Установка

```bash
cd /root/plugin-recorder-Nginx
bash install.sh --install /root/answers.env
```

Что произойдёт по порядку:

1. проверки шага 4 повторяются, при блокерах установка останавливается;
2. создаются каталоги `${PLUGIN_DIR}/{output,public,profile,state,backups,ca}`;
3. собираются образы `recorder-plugin/{controller,portal,publisher,recorder}`;
4. поднимается compose-проект `recording-overlay`;
5. каждый воркер проверяется на `/health`;
6. генерируется nginx-сниппет, при host-nginx — с `nginx -t` и reload;
7. модуль кнопки регистрируется в Element Web.

## Шаг 6. Проверка после установки

```bash
# контроллер видит воркера
curl -s http://127.0.0.1:8765/health | python3 -m json.tool

# ожидается: "workers": [{"ok": true, ...}]

# nginx отдаёт маршрут
curl -sk -o /dev/null -w '%{http_code}\n' https://ВАШ_ДОМЕН/recording/health
# ожидается: 200

# страница записей
curl -sk -o /dev/null -w '%{http_code}\n' https://ВАШ_ДОМЕН/recordings/
# ожидается: 200
```

Затем в Element Web откройте комнату, запустите звонок и нажмите кнопку
записи. Через 10–15 секунд:

```bash
curl -s "http://127.0.0.1:8765/api/record/status?roomId=КОМНАТА" \
     -H "Authorization: Bearer ТОКЕН" | python3 -m json.tool
```

Остановите запись и проверьте файл:

```bash
find /opt/recorder-plugin/output -name '*.mp4' -newermt '-15 minutes' \
     -printf '%s %p\n'
```

Ненулевой размер и есть результат. Если файл есть, но пустой — смотрите
`docker logs rec-worker-1`, там будет причина от ffmpeg.

## Шаг 7. Если что-то не так

```bash
# состояние всех сервисов
docker compose -p recording-overlay \
    -f /opt/recorder-plugin/state/compose.yaml ps

# логи по слоям
docker logs --tail 50 rec-controller
docker logs --tail 50 rec-worker-1
docker logs --tail 50 rec-portal

# повторная проверка окружения
bash install.sh --check /root/answers.env

# переустановка модуля UI и маршрутов без пересборки
bash install.sh --repair /root/answers.env
```

## Удаление

```bash
bash install.sh --uninstall /root/answers.env
```

Записи в `RECORDINGS_DIR` и профиль бота `profile-spk` сохраняются намеренно:
в профиле лежит E2EE-сессия, и её потеря означает, что бот вступит в комнаты
как новое устройство.

## Замечания по чистому снепшоту

Если сервер развёрнут из снапшота, где плагин уже пытались ставить, шаг 3
обязателен: аккаунты и порты могли остаться. Проверка шага 4 покажет это
явно — `..` для своих контейнеров и `!!` для посторонних `rec-*`.

Если на снапшоте остался старый nginx-конфиг плагина
(`/etc/nginx/conf.d/recorder-plugin.conf`), он будет заменён целиком: блок
обрамлён маркерами, дублей не появится.

Если остался каталог `/opt/recorder-plugin`, установщик переиспользует его:
записи и профиль бота не пострадают. Пересоздать с нуля:

```bash
docker compose -p recording-overlay \
    -f /opt/recorder-plugin/state/compose.yaml down --remove-orphans
mv /opt/recorder-plugin /opt/recorder-plugin.bak-$(date +%F)
```
