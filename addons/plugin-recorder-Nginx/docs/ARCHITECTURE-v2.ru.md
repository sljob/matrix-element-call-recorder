# plugin-recorder v2 — архитектурный вывод и план переписывания

Статус: проект решения. Цель — кнопка записи в Element Web как **официальный
ESM-модуль Module API** (`api.extras.addRoomHeaderButtonCallback`), а не
оверлей/инъекция в DOM. Все факты ниже проверены по исходникам тега
`element-hq/element-web@v1.12.23` и типам пакета
`@element-hq/element-web-module-api` (версии 1.12.0, 1.13.0, 1.14.0, 2.3.0).

---

## 1. Диагноз: почему кнопка 0.1.x не появляется

| # | Причина | Где |
|---|---------|-----|
| 1 | `record-button.js` ищет контейнеры `[data-testid="call-controls"]`, `.mx_CallView_controls`, `[class*="CallView"]`… В 1.12.23 звонок — это **iframe Element Call (виджет MatrixRTC)**; таких узлов в DOM Element Web нет → `inCall()` = false → `insertButton()` выходит до вставки. В консоли это видно как бесконечное `room id not found yet; will retry on DOM change`. | `src/ui/record-button.js:370-416` |
| 2 | Когда якорь всё же найден (`mx_LegacyCallViewHeader_controls`, `.mx_RoomHeader_buttons`), узел вставляется `appendChild` **внутрь React-дерева**. React 19 владеет этими детьми и при следующем reconcile удаляет чужой узел. Отсюда «вставлена → исчезла». | тот же файл |
| 3 | `discoverRoomId()` берёт id из `location.hash` регуляркой `[!#]…:домен` — для id с `/` и `=` ломается; статус опрашивается **без Authorization**, а эталонный контроллер требует Bearer на `/api/record/status` → 403, кнопка вечно «Проверка…». | `record-button.js:134-153, 258-280` vs корень `src/controller/app.py:306-317` |
| 4 | Доставка — `<script>` в `index.html` через `docker cp`. Теряется при пересоздании контейнера; требует `--reapply-ui`; не проверяется, что файл реально отдаётся. | `installer/install.py:400-474` |
| 5 | Маршрутизация — только nginx-лейблы. Стенд из монолитного `install.sh` использует **только file-provider** (`providers.file.filename=/config/dynamic.yml`), поэтому `check_routing_prereqs()` корректно отказывает, и плагин на нашем же стенде не ставится. При этом маршруты `/recording` и `/recordings` там **уже существуют** — их надо переиспользовать, а не создавать. | `installer/install.py:678-700`; корень `installer/install.py:838, 867-870` |

Рабочая кнопка в монолитном референсе (`config/record-button.js` в корне) живёт
потому, что это `position:fixed` панель **вне** React-дерева. Это работает, но
это оверлей, а не интеграция: не следует теме, не попадает в заголовок, не
локализуется средствами Element.

> `DEFECTS.md` (D2, D12, D15, D24) в репозитории **отсутствует** — ни в корне,
> ни в подпапках. Добавьте файл в корень, и я сопоставлю дефекты с планом.
> Пока учтены четыре дефекта из `addons/plugin-recorder/CHANGELOG.md` 0.1.1
> (перехват `/recordings`, docker-provider, TLS default cert, язык по умолчанию).

---

## 2. Официальный механизм (проверено по коду 1.12.23)

### 2.1 Загрузка модулей
`apps/web/src/vector/init.tsx::loadPlugins()`:

```ts
window.React = React;                         // модуль НЕ тащит свой React
const modules = SdkConfig.get("modules");     // config.json → "modules": [url, …]
const moduleLoader = new ModuleLoader(ModuleApi.instance);
for (const src of modules) {
    const module = await import(/* webpackIgnore: true */ src);   // динамический ESM import
    await moduleLoader.load(module);          // semver: satisfies(__VERSION__, module.moduleApiVersion)
}
await moduleLoader.start();                   // → module.load()
```

Контракт модуля (`packages/module-api`):

```ts
export default class RecorderModule {
    static moduleApiVersion = ">=1.12.0 <3.0.0";   // диапазон semver
    constructor(api: Api) {}
    async load(): Promise<void> {}
}
```

Версии API в образах `vectorim/element-web`:

| Element Web | module-api | `/docker-entrypoint.d/18-load-element-modules.sh` |
|---|---|---|
| ≤ 1.12.14 | нет (pre-monorepo) | нет |
| 1.12.16 – 1.12.20 | 1.13.0 | есть |
| 1.12.22 – 1.12.23 | **1.14.0** | есть |
| npm latest | 2.3.0 | — |

`addRoomHeaderButtonCallback` присутствует во всех 1.12.0 … 2.3.0.

### 2.2 Кнопка в заголовке комнаты
`ExtrasApi.addRoomHeaderButtonCallback(cb: (roomId) => JSX.Element | undefined)`.
`RoomView.render()` (строки 2706-2741) собирает результаты всех колбэков и
передаёт `<RoomHeader extraButtons={<>{…}</>}>`; в `RoomHeaderButtons` они
рендерятся **первыми**, слева от кнопок звонка (`RoomHeader.tsx:300-302`).
Элемент полностью управляется React → **не может быть стёрт** reconcile'ом.
Колбэк вызывается при каждом рендере RoomView — компонент должен быть
идемпотентным; состояние — в module-scope store + `useSyncExternalStore`.

### 2.3 Доставка в Docker-образ
Официальный образ (`apps/web/Dockerfile`, `docker/`):
* `/docker-entrypoint.d/18-load-element-modules.sh` при старте копирует
  `/app/config*.json` → `/tmp/element-web-config/` и для каждого
  `/modules/<NAME>/` дописывает `"modules" += ["/modules/<NAME>/<main>"]`
  (`main` из `package.json`, по умолчанию `index.js`); `jq`+`sponge` в образе есть.
* nginx: `location /config { root /tmp/element-web-config; }` и
  `location /modules { alias /modules; }`.
* Следствие: достаточно положить файлы в `/modules/recorder/` контейнера и
  перезапустить его. **`/app/config.json` хоста не трогаем** — даже если он
  примонтирован `:ro`.

### 2.4 Что ещё даёт `Api` (пригодится)
`api.i18n.language` (язык пользователя), `api.profile` (Watchable: userId,
isGuest), `api.config.get(key)` (ключи в reverse-DNS), `api.createRoot()`,
`api.openDialog()`, `api.navigation.openRoom()`, `api.client.getRoom(id)`.
Access token официально не отдаётся — берём
`window.mxMatrixClientPeg.get().getAccessToken()` (выставляется в
`MatrixClientPeg.ts:487-488`), fallback `localStorage.mx_access_token`.
