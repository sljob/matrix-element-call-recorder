# Что именно взято из монолита

Источник: `https://github.com/sljob/matrix-element-call-recorder`, commit `4b2aa7eb421b90beaf917202bc05eea20f8a388b`.

Прослежена вся цепочка, включая **последующие перезаписи**, а не только первая генерация шаблона:

1. `install.sh` получает `SOURCE_COMMIT`, проверяет SHA256 по `installer/sources.json`, запускает `installer/install.py`.
2. В районе строки 505 `installer/install.py` генерирует observer из `installer/templates/recorder-observer.js`, добавляет fetch login/join, остановку и очистку RTC membership.
3. Блок `INSTALLER_FIXED_FILES_V09` затем перезаписывает ряд файлов версиями из `installer/compat/`.
4. **Финальный блок `PARALLEL_RECORDING_RELEASE_V1`, строки 1099–1106, снова перезаписывает recorder файлы содержимым `src/recorder/*`. Именно это — финальный источник перед build.**
5. Далее выполняется локализация fallback URL/строк, синтаксические проверки и сборка образа. Менять только первоначальный template недостаточно.

В `vendor/monolith/p63-observer.js` скопирован **финальный `src/recorder/p63-observer.js`**, а не template или прежний vendor Nginx-плагина. SHA256 зафиксирован в `vendor/PROVENANCE.json`.

Сохранены:

- `REC_USER`, `REC_PASS`, `REC_DEVICE_ID`, постоянный каталог `/work/profile-spk`;
- вход Matrix и join той же последовательностью;
- localStorage/Matrix SDK/браузерная криптобаза;
- WebSocket rewrite для внутреннего LiveKit;
- параметры Chromium, кроме явно перечисленных интеграционных дополнений;
- gate медиапотоков и аргументы FFmpeg, включая fragmented MP4;
- обработка SIGINT/SIGTERM, закрытие браузера и очистка RTC membership.

Адаптер `installer.adapt_observer()` меняет только executable path, SPKI/host-resolver args и доверенный локальный Element Call origin. Автотест сравнивает byte-for-byte блок авторизации и весь код начиная с `// INJECT`, включая захват и остановку. Никаких переопределений MIN_WAIT из прежних серверных скриптов нет.

От монолита отличаются интеграционные части:

- один worker работает с `network_mode: host`; его HTTP API слушает только loopback;
- Node HTTP adapter вместо pool router и прежнего recorder server;
- backend на стандартной библиотеке Python + SQLite вместо старого Flask controller;
- приватный portal вместо статического autoindex;
- Nginx locations вместо Traefik labels;
- публикация запускается только по завершению observer; вместо сканирования растущих файлов используется `.done` и metadata commit.

Не утверждается, что GitHub commit был проверен на текущем тестовом сервере в этой сессии. Пользовательский факт работоспособности монолитного установщика принят как исходная опора; сквозная проверка новой комбинации обязательна. Автоматического переключения аккаунта `spk` на `recorder`, повышения power levels, переноса чужого профиля и сброса IndexedDB проект не выполняет.
