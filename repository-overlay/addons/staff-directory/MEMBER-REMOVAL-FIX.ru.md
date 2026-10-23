# Обновление опубликованного комплекта: исключение участников

База: 4deb17e8ddb20d7bd8c7cbb8e3f33f6385d0839e. Это исправление комплекта0.1.6, не новая установка на Cloud.

Новые комнаты создаются с kick99. Установленный Cloud уже исправлен скриптом27. Для другого ранее установленного экземпляра: `bash addons/staff-directory/enable-member-removal.sh` из полной локальной копии исходников. Скрипт не исключает людей автоматически, не снимает ban100/redact100, не разрешает писать сообщения. Команда восстановления с --restore сохраняется в выводе. Если patch уже есть — изменений нет.

В GitHub Desktop: Fetch/Pull → Repository/Show in Explorer. Содержимое repository-overlay из ZIP скопировать в корень клона, заменяя только включённые файлы. Сохранить свои незакоммиченные изменения заранее. Если HEAD уже отличается от указанной базы, сверить приложенный diff — не затирать чужие изменения.

Включены полные README.md/README.ru.md и addons README RU/EN: описание записи, ACL архива, LLM-авторства, статусов и флага установки сохранено. Новое описание посвящено исключению пользователей и предупреждению не деактивировать Recorder.

Заменяются provision.py, standalone.sh, test_cloud.py, TEST-RESULTS.txt, README RU/EN (корень и addon), installer/sources.json, SHA256SUMS. Добавлены enable-member-removal.sh, member-removal.py, эта инструкция. answers.example и bootstrap install.sh не меняются: STAFF_DIRECTORY_ENABLED=true/false уже опубликован и остаётся рабочим.

Из корня клона выполнить `py tools/validate-staff-release.py`. Если файлы редактировались: сначала `py tools/update-staff-manifests.py`, затем validate. Эти команды работают с локальными файлами, ничего не устанавливают.

Changes → проверить diff без секретов → Summary `Fix staff room member removal permissions` → Commit → Push origin. Удобно сначала отдельная ветка fix/staff-member-removal, затем Pull Request. provision.py, standalone.sh и sources.json опубликовать вместе одним коммитом. Для новых серверов брать SOURCE_COMMIT нового коммита. Существующий Cloud answers.conf не менять.

Зачем standalone пересобран: простое исправление provision.py не меняет закодированную в standalone копию, она могла бы продолжить создавать kick100. sources.json тоже требуется пересчитать, иначе bootstrap остановится с checksum mismatch. В комплекте всё согласовано.

Не заявляется запрет записи в комнате Сотрудники: кнопка Запись всё ещё может пригласить Recorder. На Cloud больше запускать ничего не нужно. В чате опубликованные исходники не изменялись/не пушились.
