# Как проверить плагин перед публикацией

Проблема: тестовый сервер уже полностью настроен (запись там есть), а второй чистый
сервер — дорого. Ниже четыре варианта от самого дешёвого к самому честному. Нужны не все:
варианты 0, 1 и 2 отвечают на разные вопросы и вместе покрывают плагин хорошо.

| Вариант | Время | Что доказывает | Что нужно |
|---|---|---|---|
| 0. Без сервера | 5 мин | синтаксис, генерация compose, отказы | только git-клон |
| 1. Фейковый стек | 10 мин | обнаружение, маршруты, вставка кнопки | Docker на любой машине |
| 2. Adopt на тестовом сервере | 15 мин | маршруты и кнопка поверх реального рекордера | ваш meet.example.org |
| 3. Клон тестового сервера без записи | 30–60 мин | полный цикл установки и реальная запись | ВМ-клон или бэкап |
| 4. Чистая ВМ | 2–3 ч | установка на сервер, который никогда не видел плагин | любой VPS 4 ГБ |

---

## Вариант 0. Без сервера (5 минут)

Ничего не запускает. Проверяет, что файлы хотя бы не сломаны.

```bash
cd matrix-element-call-recorder-plugin
bash -n install.sh tools/fetch-upstream.sh testbed/smoke.sh
python3 -m py_compile installer/install.py src/controller/app.py \
    src/portal/index.py src/publisher/publish.py src/adopt/proxy.py
node --check src/ui/record-button.js
node --check src/recorder/server.js
python3 -m json.tool locales/en.json >/dev/null && echo "locales ok"
```

Теперь убедитесь, что установщик **отказывается**, а не делает половину работы:

```bash
# пустой DOMAIN → ненулевой код
sed 's/^DOMAIN=.*/DOMAIN=/' answers.example > /tmp/a1.conf; chmod 600 /tmp/a1.conf
sudo bash install.sh --check /tmp/a1.conf; echo "rc=$?  (ожидается не 0)"

# answers с правами 644 → отказ ещё до Python
cp answers.example /tmp/a2.conf; chmod 644 /tmp/a2.conf
sudo bash install.sh --check /tmp/a2.conf; echo "rc=$?  (ожидается 1)"

# ветка вместо SHA → отказ
RECORDER_UPSTREAM_COMMIT=main bash tools/fetch-upstream.sh; echo "rc=$? (ожидается 1)"
```

---

## Вариант 1. Фейковый стек: nginx + Element Web (10 минут)

Самый полезный тест именно для *этого* плагина, потому что плагин — почти целиком
интеграция: найти nginx, найти его сеть, найти Element Web, вставить кнопку, добавить
маршрут. Ни для чего из этого Matrix не нужен.

Стенд слушает порты 8090/8443, чтобы не мешать настоящему nginx, если проверка идёт
на том же хосте.

```bash
cd matrix-element-call-recorder-plugin/testbed
docker compose -f compose.fake.yml up -d
sudo bash smoke.sh          # до установки: часть проверок помечена «ещё не»
```

Затем полный цикл (smoke использует режим adopt, поэтому ядро рекордера не нужно):

```bash
sudo bash ../install.sh --install /tmp/answers-smoke.conf
sudo bash smoke.sh          # после установки: все проверки должны пройти
```

| Проверка | Что ловит |
|---|---|
| фейковый стек поднят | сломанный стенд |
| сеть nginx обнаружена | ошибки автоопределения сети |
| `--check` выходит с 0 | ошибки разбора answers |
| `/recording/health` = 404 до установки | доказывает, что маршрут наш |
| `record-button.js.patch.js` отдаётся с баннером | вставка и раздача файла |
| `POST /start` без токена = 401 | **открытый порт воркера** — критично |

Посмотреть кнопку глазами: добавьте `meet.localhost` в `/etc/hosts` на своей машине,
откройте `https://meet.localhost:8443/` с `Ctrl+Shift+R` и смотрите строки `[recorder]`
в консоли браузера.

Убрать стенд:

```bash
sudo bash ../install.sh --uninstall /tmp/answers-smoke.conf
docker compose -f compose.fake.yml down
```

---

## Вариант 2. Режим adopt на вашем тестовом сервере (15 минут)

У вас на meet.example.org запись уже есть. Это не мешает: плагин умеет **подключаться**
к существующему рекордеру и проверяет при этом ровно те части, которые отличают его от
основного установщика — обнаружение, маршруты и вставку кнопки.

Что произойдёт на сервере: появится один контейнер `rec-adopt-proxy`, два маршрута
nginx и один тег `<script>` в `index.html` Element Web. Существующие контейнеры не
перезапускаются. `--uninstall` возвращает всё назад.

**Важно:** если у вас уже есть маршрут `/recording` на существующий контроллер, новый
маршрут с приоритетом 100 перекроет его. Это ожидаемо для теста, но знайте об этом.

```bash
cat > /root/answers-adopt.conf <<'EOF'
DOMAIN=meet.example.org
PLUGIN_DIR=/opt/recorder-plugin
INSTALL_LANGUAGE=ru
RECORDER_ACCOUNTS=@recorder:meet.example.org
RECORDER_PASSWORDS=
TLS_CA_FILE=/opt/element-stack/certs/ca.crt
ADOPT_EXISTING=true
ADOPT_CONTROLLER_URL=http://host.docker.internal:8765
ADOPT_PORTAL_URL=http://host.docker.internal:8899
EOF
chmod 600 /root/answers-adopt.conf

bash install.sh --check   /root/answers-adopt.conf     # прочитать план
bash install.sh --install /root/answers-adopt.conf
curl -sk https://meet.example.org/recording/health | jq
bash install.sh --uninstall /root/answers-adopt.conf
```

Если контроллер слушает не `127.0.0.1:8765`, а что-то другое, поправьте
`ADOPT_CONTROLLER_URL`. Это узнаётся из `ss -lntp | grep 8765`.

---

## Вариант 3. Клон тестового сервера с выключенной записью (30–60 минут)

Самое близкое к реальности. Нужен сервер, где записи **ещё нет** — а у вас есть сервер,
где она уже есть. Значит: клонировать и выключить только компоненты записи.

Если сервер виртуальный — snapshot или clone ВМ. На железе подойдёт ваш
`matrix-clone-*.tar.gz` на другую машину. **Никогда на production-хосте.**

Только на клоне:

```bash
systemctl stop rec-recorder.service rec-http.service rec-publish.service rec-watch.service 2>/dev/null || true
systemctl disable rec-recorder.service rec-http.service rec-publish.service rec-watch.service 2>/dev/null || true
docker stop recorder-p44 recording-controller record-plugin-static 2>/dev/null || true
docker rm   recorder-p44 recording-controller record-plugin-static 2>/dev/null || true

# остальное должно остаться живым:
curl -sk https://meet.example.org/_matrix/client/versions | head -c 120; echo
docker ps --format '{{.Names}}' | grep -E 'synapse|nginx|element|livekit'
```

Теперь это ровно тот сервер, для которого написан плагин:

```bash
export RECORDER_UPSTREAM_COMMIT=<sha>
bash tools/fetch-upstream.sh
bash install.sh --check   /root/answers-plugin.conf
bash install.sh --install /root/answers-plugin.conf
```

Если у вас остался `profile-spk` старого рекордера, его **можно** скопировать в
`<PLUGIN_DIR>/profile/<user_safe>/` до первого запуска — тогда вход в Matrix не
понадобится и ключи E2EE сохранятся.

### Сценарий приёмки: две машины, две учётки

1. Учётка A (уровень прав ≥ 50) создаёт комнату и начинает звонок.
2. Нажать «Запись». Рекордер должен войти участником за 3–5 с.
3. Говорить 30–60 с, показать экран, говорить ещё 20 с.
4. Нажать «Стоп». Кнопка переходит в «Обработка…», потом снова в «Запись».
5. Открыть «Записи»: файл в списке, MP4 воспроизводится, MP3 скачивается, JPG есть.
6. Учётка B, **не** член комнаты: открыть URL файла → обязан быть 403.
7. Учётка B входит в комнату с уровнем 0 → видит в архиве только свои комнаты и
   **не может** начать запись (403 от контроллера).
8. Учётка B с подделанным запросом: `curl -k -X POST .../recording/api/record/start
   -H 'Content-Type: application/json' -d '{"roomId":"!...","powerLevel":100}'` без
   заголовка Authorization → 401. С токеном B → 403. Это проверяет, что контроллер
   не верит телу запроса.

```bash
ffprobe -v error -show_entries format=duration:stream=codec_type,codec_name \
        -of default=nw=1 /opt/recorder-plugin/public/*/*.mp4 | head -20
```

Ожидается видео- и аудиопоток, длительность > 0.

---

## Вариант 4. Чистая ВМ (2–3 часа, самый честный)

Нужен только если хотите проверить сервер, который никогда не видел Element. Достаточно
VPS на 4 ГБ и домена; `nip.io`/`sslip.io` заменяют настоящий домен
(`meet.203.0.113.10.sslip.io`).

Соберите минимальный рабочий сервер вашим основным установщиком, затем **удалите из него
запись** командами из варианта 3 и проверьте, что плагин возвращает её обратно. Так
подтверждается, что плагин ничего не предполагает о состоянии хоста.

---

## Что считать «готово к публикации»

Минимум, после которого можно нажимать Push:

- [ ] вариант 0 проходит целиком;
- [ ] вариант 1: `smoke.sh` зелёный после `--install`, `--uninstall` восстанавливает
      `index.html` (проверить `docker exec fake-element-web grep -c recorder-plugin
      /usr/share/nginx/html/index.html` → 0);
- [ ] вариант 2 или 3: кнопка видна в реальном Element, `/recording/health` отвечает
      через nginx;
- [ ] вариант 3, шаги 6–8 сценария приёмки: три отказа (403, 403, 401) получены.

Если какой-то пункт не выполнен — так и напишите в README в разделе «Честный статус».
Публикация не является доказательством проверки.
