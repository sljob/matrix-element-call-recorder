# Публикация плагина на GitHub через GitHub Desktop

Пошагово, без командной строки для git. Команды в терминале нужны только для сборки
пакета — их можно выполнить в PowerShell или в Git Bash.

---

## 1. Куда именно выкладывать: новая папка в вашем аккаунте

Плагин — это **отдельный репозиторий**, а не папка внутри основного.

```
github.com/sljob/matrix-element-call-recorder          ← уже есть, не трогаем
github.com/sljob/matrix-element-call-recorder-plugin   ← новый, сюда
```

Почему отдельный репозиторий, а не подпапка основного:

- у него свой жизненный цикл: плагин ломается от обновления Element Web, основной
  установщик — нет;
- пользователи клонируют именно его; `SOURCE_COMMIT` основного проекта к нему не
  применяется;
- основная ветка не получает коммитов, которые меняют установщик сервера.

Имя `matrix-element-call-recorder-plugin` рядом с основным читается однозначно и
находится поиском. Если хотите другое — подойдёт любое, кроме совпадающего с основным.

---

## 2. Подготовить папку на диске

Распакуйте присланный архив плагина так, чтобы получилось:

```
C:\Users\<вы>\Documents\GitHub\matrix-element-call-recorder-plugin\
├── .gitignore
├── LICENSE
├── README.md
├── README.ru.md
├── answers.example
├── install.sh
├── installer\
│   └── install.py
├── src\
│   ├── adopt\
│   ├── controller\
│   ├── portal\
│   ├── publisher\
│   ├── recorder\
│   └── ui\
├── locales\
├── tools\
├── testbed\
├── docs\
└── vendor\
```

`vendor\` в архив не входит и в git не попадает: там окажется закреплённое ядро
рекордера, которое каждый пользователь скачивает сам. Версию ядра фиксирует
`vendor\SOURCE`, и вот **его** коммитить нужно.

---

## 3. Проверить, что ничего лишнего не попадёт в репозиторий

`.gitignore` в пакете уже закрывает профили, записи, логи и `answers*.conf`. Проверьте
глазами в GitHub Desktop на вкладке **Changes**, что в списке нет:

- `answers-plugin.conf`, `answers-adopt.conf` — пароли рекордеров;
- `profile\`, `storageState.json` — ключи E2EE;
- `*.mp4`, `*.mp3` — записи;
- `vendor\*.js` (должен быть только `vendor\SOURCE`).

Если что-то из этого видно — не коммитьте, сначала поправьте `.gitignore`.

---

## 4. Создать репозиторий в GitHub Desktop

1. **File → Add local repository…**
2. Укажите папку `matrix-element-call-recorder-plugin`.
3. GitHub Desktop скажет, что это не git-репозиторий, и предложит **create a
   repository** — согласитесь.
4. Заполните:
   - **Name:** `matrix-element-call-recorder-plugin`
   - **Description:** `Record button for E2EE Element Call conferences on an already running server`
   - **Local path:** родительская папка (`Documents\GitHub`)
   - **Initialize with README:** ❌ выключить (README у нас свой)
   - **Git ignore:** `None` (наш `.gitignore` уже на месте)
   - **License:** `None` (наш `LICENSE` уже на месте)
5. **Create repository**.

Если пункт 3 не появился: **File → New repository…**, укажите то же имя и путь
`Documents\GitHub`, и создайте пустой репозиторий **рядом** с папкой, затем перенесите
файлы внутрь.

---

## 5. Первый коммит

Вкладка **Changes** покажет все файлы. В поле **Summary**:

```
Plugin 0.1.0 — record button for an existing Element Call server
```

В **Description**:

```
Adds recording to a running Element Web / Element Call stack without
touching its compose file, traefik.yml or homeserver.yaml.

- overlay compose project: controller, worker pool, publisher, portal
- Traefik routes via container labels only
- record button injected into Element Web, backed up first
- start/stop authorized against Synapse, not against the request body
- adopt mode for servers that already run a recorder
- answers.example, ru/en README, testing guide, fake-stack smoke test
```

Нажмите **Commit to main**.

---

## 6. Опубликовать

**Publish repository** сверху.

- **Keep this code private** — снимите галочку (это публичный плагин).
- **Publish repository**.

---

## 7. Сразу после публикации

### 7.1. Узнать SHA и вписать его в README

В GitHub Desktop: **History → правый клик по коммиту → Copy SHA**.

Допишите в конец `README.md` и `README.ru.md` раздел с примером:

```bash
export RECORDER_UPSTREAM_COMMIT=<40-символьный SHA основного репозитория>
```

Важно: это SHA **основного** репозитория (`matrix-element-call-recorder`), а не
плагина. Ветку `main` указывать нельзя — скрипт её отвергнет.

### 7.2. Создать релиз

На странице репозитория: **Releases → Create a new release**

- **Tag:** `v0.1.0`
- **Title:** `0.1.0 — first public version`
- **Description:** коротко: что умеет, что проверено, что не проверено (честно, как в
  разделе «Честный статус» в README).

Архив исходников GitHub соберёт сам, прикладывать zip не нужно.

### 7.3. Проверить, что клонируется

На другой машине или в пустой папке:

```bash
git clone https://github.com/sljob/matrix-element-call-recorder-plugin.git
cd matrix-element-call-recorder-plugin
bash -n install.sh && python3 -m py_compile installer/install.py && echo OK
```

Это вариант 0 из `docs/TESTING.ru.md`. Он должен пройти на чистой машине без Docker.

---

## 8. Как обновлять потом

1. Правите файлы в той же папке.
2. GitHub Desktop → **Changes** → Summary → **Commit to main**.
3. **Push origin**.
4. Если изменилось поведение — поднимите `PLUGIN_VERSION` в
   `installer/install.py` и заодно тег релиза.

Обновление `vendor/SOURCE` — отдельный коммит с сообщением вида
`pin recorder core at <sha>`, чтобы по истории было видно, какое ядро проверялось.

---

## 9. Чего не делать

- Не заливайте `answers.conf`, профили рекордеров и записи — даже в private-репозиторий.
- Не добавляйте `vendor/*.js` в коммит: это чужая копия, которая начнёт расходиться
  с основным проектом.
- Не переносите папку плагина внутрь папки основного репозитория: GitHub Desktop начнёт
  показывать её как часть основного.
- Не публикуйте как «проверено на чистой ВМ», если прогоняли только вариант 1.
