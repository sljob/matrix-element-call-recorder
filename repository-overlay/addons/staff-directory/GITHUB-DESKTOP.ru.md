# Что куда положить — GitHub Desktop

Комплект staff-directory0.1.6 подготовлен для слепка репозитория e374a2de4634220e99e01396b2e4fb876fb4c35c. Этот HEAD проверен при подготовке. Публикация не выполнялась из чата. Cloud уже работает: повторная установка не нужна.

## 1. Подготовка локального клона

GitHub Desktop → выбрать sljob/matrix-element-call-recorder → Fetch origin → Pull origin. Сохранить локальные изменения (commit/ветка), затем Branch → New branch → feature/staff-directory. Repository → Show in Explorer откроет корень клона. Если remote успел измениться после указанного SHA, не затирать новые install.sh/README/manifest вслепую — сопоставить diff с приложенным staff-directory.patch.

## 2. Копирование

Распаковать staff-directory-github-0.1.6.zip. Внутри repository-overlay. Копировать СОДЕРЖИМОЕ repository-overlay В КОРЕНЬ клона, не саму папку repository-overlay. Например addons/staff-directory/install.sh должно оказаться в <корень>/addons/staff-directory/install.sh, не в <корень>/repository-overlay/...

Добавить:
- addons/staff-directory/ целиком: комплектующие, отдельный install.sh и standalone.sh, исходники, acceptance, тесты, документация.
- tools/update-staff-manifests.py и tools/validate-staff-release.py.

Заменить только одноимённые файлы:
- answers.example — STAFF_DIRECTORY_ENABLED=false;
- install.sh — bootstrap, не engine installer/install.py;
- installer/sources.json — хеши исходников, включая addon;
- README.md (English) и README.ru.md (русский) — полный существующий текст сохранён, addon раздел обновлён;
- SHA256SUMS — актуальные контрольные суммы.

Ничего не удалять из старых src/config/installer. Engine installer/install.py и recorder исходники не меняются. Не копировать START-HERE, zip, server logs или production config в корень. Отдельный автономный patch — addons/staff-directory/standalone.sh (на другом сервере БЕЗ addon). patches/26... — исторический адресный update исходного Cloud0.1.5, не для текущего Cloud.

## 3. Проверка и публикация

Из корня клона на Windows: `py tools/validate-staff-release.py` (или python3 на Linux). Только проверяет локальные файлы, не разворачивает сервер. Если правили addon/документацию: сначала `py tools/update-staff-manifests.py`, затем validate. Утилита также пересобирает standalone.sh из runtime исходников, чтобы он не отличался от каталога. После проверки не редактировать файлы без повторного пересчёта.

GitHub Desktop Changes: ожидается новая addon папка/tools и root файлы выше. Нет .cred, private keys, токенов, answers.conf, state, БД и серверных логов. Summary: `Add optional encrypted staff directory 0.1.6`. Commit to feature/staff-directory → Publish branch/Push origin → Create Pull Request → проверить diff → merge. Можно публиковать через вашу обычную ветку, но все bootstrap/addon/sources.json обязаны попасть в ОДИН commit. Не делать сначала bootstrap, потом manifest.

После merge получить полный40-символьный SHA коммита со всеми исходниками. Для НОВОГО сервера указать его в SOURCE_COMMIT, выбрать STAFF_DIRECTORY_ENABLED=true либо false. Не пытаться вшить SHA будущего коммита в него самого; в answers.example оставлен placeholder. --check не создаёт addon; --install запускает его после готовности основного стека, затем интерактивно запросит admin токен. В production Cloud существующий answers.conf НЕ редактировать и основной installer НЕ запускать.

## 4. Что считается проверенным

Cloud: установлен0.1.5, успешно применён26 patch (ACL/readback/type/active services). Meet: прежняя логика4/4. Все эти факты отражены в README. Полный Cloud4/4 лог не получен; optional acceptance есть в папке. Новая VM с true-флагом ещё не развёртывалась: новая интеграция bootstrap и новая политика создания bot-only проверены автоматическими/mock тестами, но не end-to-end. Не объявлять fresh-install полностью проверенной; после тестовой VM можно обновить validation section и создать Release. Работающая Cloud-конференция не доказывает fresh-install путь.

## 5. Отдельное развёртывание похожей комнаты

Скачать standalone.sh и выполнить от root на работающем сервере, где справочник НЕ установлен. Потребуются DOMAIN, существующий INSTALL_DIR и admin token скрытым вводом. Создаст одну новую комнату/бота; в неё приглашать сотрудников. Повторные комнаты/боты на том же сервере этим пакетом не поддерживаются. Самостоятельный установщик не снимает ограничения прав администратора99.
