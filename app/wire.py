"""Связывание сервисов между собой через их API.

То, что человек иначе делает руками полчаса: сказать Prowlarr про Sonarr и Radarr,
показать обоим торрент с паролем, задать корневые папки и включить hardlink'и.
Каждый шаг — несколько кликов в чужом интерфейсе с копированием ключей API из
одного окна в другое, и каждый можно молча сделать не так.

Ключи API нигде не спрашиваются: *arr держат их в своих config.xml, и эти файлы
лежат в папке настроек на этой же машине. Читает их media.arr_api_key.

Три принципа, от которых тут нельзя отступать:

1. **Идемпотентность.** Кнопку нажмут дважды. Каждый шаг сначала смотрит, нет ли
   уже такой записи, и только потом создаёт. Иначе в Sonarr появятся три торрента.
2. **Схему просим у сервиса, а не выдумываем.** У Sonarr, Radarr и Prowlarr поля
   настроек описаны в /schema, и набор полей меняется между версиями. Захардкоженный
   набор однажды разъедется с сервисом и даст ошибку, в которой не будет ни слова
   о причине.
3. **Внутренние адреса, а не внешние.** Сервисы ходят друг к другу по k8s-DNS и
   родным портам (http://sonarr:8989). Подставить сюда localhost:30989 —
   работающий с виду адрес, который не найдётся изнутри пода.
"""

from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config
import media

# Категории раздач, которые Prowlarr отдаёт каждому приложению. Числа — из его же
# справочника: 5000 это «ТВ» целиком, 2000 — «Фильмы». Без них синхронизация
# индексаторов создаётся, но не ищет ничего.
SYNC_CATEGORIES = {"sonarr": [5000, 5010, 5020, 5030, 5040, 5045, 5050],
                   "radarr": [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070]}


class Step:
    """Результат одного шага — то, что покажется человеку строкой в окне."""

    def __init__(self, title: str, ok: bool, detail: str) -> None:
        self.title, self.ok, self.detail = title, ok, detail

    def __repr__(self) -> str:
        return f"{'OK ' if self.ok else 'НЕТ'} {self.title}: {self.detail}"


# --- Обёртки над API --------------------------------------------------------

def _reason(raw: bytes) -> str:
    """Достаёт из тела ответа причину, которую назвал сам сервис.

    Sonarr и Radarr на отказ отвечают не пустой четырёхсоткой, а списком вида
    [{"propertyName": "Path", "errorMessage": "Folder is not writable by user abc"}].
    Без этой строки в окне остаётся "сервис ответил 400" — сообщение, по которому
    нельзя ни понять причину, ни её починить. Права на папку выглядят именно так.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return raw.decode("utf-8", "replace").strip()[:200]
    items = payload if isinstance(payload, list) else [payload]
    parts = [str(item.get("errorMessage") or item.get("message") or "").strip()
             for item in items if isinstance(item, dict)]
    return "; ".join(p for p in parts if p)[:200]


def _api(key: str, api_key: str, path: str, *, data=None, method: str | None = None):
    """Запрос к сервису снаружи, по NodePort: приложение живёт на хосте.

    Возвращает тройку (код, разобранный ответ, причина отказа). Причина пустая,
    когда всё хорошо, и разбирается из тела, когда нет.
    """
    url = f"{config.service_url(config.BY_KEY[key])}{path}"
    headers = {"X-Api-Key": api_key}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    code, raw = media._request(url, headers=headers, data=body, method=method)
    if code not in (200, 201, 202):
        return code, None, _reason(raw)
    try:
        return code, json.loads(raw) if raw else None, ""
    except ValueError:
        return code, None, ""


def _failed(title: str, code: int, reason: str) -> Step:
    """Одинаковый текст отказа во всех шагах: код и то, что сказал сервис."""
    return Step(title, False, f"сервис ответил {code}" + (f": {reason}" if reason else ""))


def wait_for_api_key(config_dir: Path, service: str, timeout: int = 120) -> str | None:
    """Ключ появляется не сразу: config.xml пишется при первом запуске сервиса,
    а не при создании контейнера. Поэтому ждём, а не проверяем один раз."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        key = media.arr_api_key(config_dir, service)
        if key:
            return key
        time.sleep(3)
    return None


def _schema_entry(key: str, api_key: str, path: str, implementation: str):
    """Берёт у сервиса готовый шаблон настроек нужной реализации.

    Именно шаблон, а не свой словарь: в нём уже проставлены configContract и
    полный список полей с их значениями по умолчанию, которые у разных версий
    разные. Нам остаётся заменить несколько значений.
    """
    _, schema, _ = _api(key, api_key, path)
    for entry in schema or []:
        if entry.get("implementation") == implementation:
            return entry
    return None


def _set_field(entry: dict, name: str, value) -> None:
    for field in entry.get("fields", []):
        if field.get("name") == name:
            field["value"] = value
            return
    entry.setdefault("fields", []).append({"name": name, "value": value})


# --- Шаги -------------------------------------------------------------------

def add_download_client(key: str, api_key: str, password: str) -> Step:
    """Торрент в Sonarr или Radarr. Без него они находят раздачу и не могут её взять."""
    title = f"Торрент в {config.BY_KEY[key].title}"
    _, existing, _ = _api(key, api_key, "/api/v3/downloadclient")
    if any(c.get("name") == "qBittorrent" for c in existing or []):
        return Step(title, True, "уже прописана")

    entry = _schema_entry(key, api_key, "/api/v3/downloadclient/schema", "QBittorrent")
    if not entry:
        return Step(title, False, "сервис не предложил настроек для qBittorrent")

    entry["name"] = "qBittorrent"
    entry["enable"] = True
    _fill_torrent_fields(entry, password)
    code, _, reason = _api(key, api_key, "/api/v3/downloadclient", data=entry)
    return (Step(title, True, "прописана") if code in (200, 201)
            else _failed(title, code, reason))


def _fill_torrent_fields(entry: dict, password: str) -> None:
    qbt = config.BY_KEY["qbittorrent"]
    _set_field(entry, "host", "qbittorrent")     # k8s-DNS, а не localhost
    _set_field(entry, "port", qbt.internal_port)
    _set_field(entry, "username", config.qbittorrent_login())
    _set_field(entry, "password", password)


def update_download_client(key: str, api_key: str, password: str) -> Step:
    """Переписывает логин и пароль в уже прописанном торренте Sonarr или Radarr.

    Нужна после смены пароля: add_download_client находит запись по имени и молча
    уходит, поэтому сам он старые данные не поправит. А со старым паролем *arr
    перестают отдавать раздачи в скачивание — молча, это видно только тем, что
    заказы висят.
    """
    title = f"Торрент в {config.BY_KEY[key].title}"
    _, existing, _ = _api(key, api_key, "/api/v3/downloadclient")
    mine = next((c for c in existing or [] if c.get("name") == "qBittorrent"), None)
    if not mine:
        return add_download_client(key, api_key, password)
    _fill_torrent_fields(mine, password)
    code, _, reason = _api(key, api_key, f"/api/v3/downloadclient/{mine['id']}",
                           data=mine, method="PUT")
    return (Step(title, True, "новые логин и пароль прописаны")
            if code in (200, 201, 202) else _failed(title, code, reason))


# Метка, которой помечается прокси. Без неё Prowlarr его просто не применяет —
# см. add_flaresolverr, там же измерения.
FLARESOLVERR_TAG = "flaresolverr"


def _ensure_tag(prowlarr_key: str, label: str) -> int | None:
    """Возвращает id метки, заводя её при необходимости."""
    _, tags, _ = _api("prowlarr", prowlarr_key, "/api/v1/tag")
    for tag in tags or []:
        if tag.get("label") == label:
            return tag["id"]
    code, created, _ = _api("prowlarr", prowlarr_key, "/api/v1/tag",
                            data={"label": label})
    return created.get("id") if code in (200, 201) and created else None


def add_flaresolverr(prowlarr_key: str) -> Step:
    """Учит Prowlarr проходить проверку Cloudflare.

    Без этого крупные трекеры недоступны совсем, и выглядит это обманчиво: сайт
    открывается у человека в браузере, а Prowlarr на том же компьютере отвечает
    «Unable to connect to indexer... 403 Forbidden». Причина не в сети и не в
    адресе — Cloudflare отдаёт страницу с задачей на JavaScript, а Prowlarr
    браузером не является. FlareSolverr эту задачу решает и отдаёт куки.

    МЕТКА НУЖНА С ОБЕИХ СТОРОН — и на прокси, и на индексере. Это измерено на
    живом Prowlarr, три состояния:

        прокси с меткой, индексер без   -> 403 за 2.2 с, FlareSolverr не тронут
        прокси с меткой, индексер с ней -> 200, в логах «Challenge solved!»
        прокси без метки, индексер без  -> 403 за 2.1 с, FlareSolverr не тронут

    То есть метка на прокси отвечает не за «включён», а за «к каким индексерам
    применять». Прокси без меток не применяется ни к одному — документация
    обещает обратное, но здесь верить измерению. Вторую половину работы —
    метку на самих индексерах — делает tag_indexers.
    """
    title = "Обход проверки «вы не робот»"
    tag_id = _ensure_tag(prowlarr_key, FLARESOLVERR_TAG)
    if tag_id is None:
        return Step(title, False, "Prowlarr не дал завести метку")

    _, existing, _ = _api("prowlarr", prowlarr_key, "/api/v1/indexerproxy")
    current = next((p for p in existing or []
                    if p.get("implementation") == "FlareSolverr"), None)
    if current:
        if current.get("tags"):
            return Step(title, True, "уже настроен")
        # Прокси есть, но без метки — значит он не работает, хотя выглядит
        # настроенным. Чиним молча: это ровно тот случай, ради которого кнопка
        # «Связать сервисы» и существует.
        current["tags"] = [tag_id]
        code, _, reason = _api("prowlarr", prowlarr_key,
                               f"/api/v1/indexerproxy/{current['id']}",
                               data=current, method="PUT")
        return (Step(title, True, "починен: без метки он не применялся")
                if code in (200, 201, 202) else _failed(title, code, reason))

    entry = _schema_entry("prowlarr", prowlarr_key, "/api/v1/indexerproxy/schema",
                          "FlareSolverr")
    if not entry:
        return Step(title, False, "Prowlarr не предложил настроек для FlareSolverr")

    entry["name"] = "FlareSolverr"
    entry["tags"] = [tag_id]
    _set_field(entry, "host", config.FLARESOLVERR_URL)
    code, _, reason = _api("prowlarr", prowlarr_key, "/api/v1/indexerproxy", data=entry)
    return (Step(title, True, "настроен: сайты за Cloudflare теперь доступны")
            if code in (200, 201) else _failed(title, code, reason))


def tag_indexers(prowlarr_key: str) -> Step:
    """Помечает уже добавленные индексеры меткой обхода Cloudflare.

    Без метки индексер идёт мимо FlareSolverr и упирается в 403 — почему именно
    так, измерено в add_flaresolverr. Индексеры человек добавляет сам и уже
    после установки, поэтому шаг делает единственное, что установщику доступно:
    помечает те, что есть на момент запуска. Добавили сайт позже — нажмите
    кнопку ещё раз, метка встанет.

    forceSave=true обязателен: обычный PUT заставляет Prowlarr заново сходить на
    сайт и проверить индексер, а это тридцать секунд против пяти в media.TIMEOUT.
    Запрос отвалился бы по таймауту, хотя метка при этом успевала сохраниться —
    молчаливое «не сработало», которого не видно ни в одном ответе.
    """
    title = "Индексеры через обход"
    tag_id = _ensure_tag(prowlarr_key, FLARESOLVERR_TAG)
    if tag_id is None:
        return Step(title, False, "Prowlarr не дал завести метку")

    _, indexers, _ = _api("prowlarr", prowlarr_key, "/api/v1/indexer")
    if not indexers:
        return Step(title, True, "пока ни одного — добавьте сайт и нажмите кнопку снова")

    marked = []
    for indexer in indexers:
        tags = indexer.get("tags") or []
        if tag_id in tags:
            continue
        indexer["tags"] = [*tags, tag_id]
        code, _, reason = _api("prowlarr", prowlarr_key,
                               f"/api/v1/indexer/{indexer['id']}?forceSave=true",
                               data=indexer, method="PUT")
        if code not in (200, 201, 202):
            return _failed(title, code, reason)
        marked.append(indexer["name"])

    if not marked:
        return Step(title, True, f"все {len(indexers)} уже помечены")
    return Step(title, True, "помечены: " + ", ".join(marked))


class _Torrent:
    """Сессия к qBittorrent: у него не ключ в заголовке, а кука.

    Отдельный маленький клиент, потому что общий помощник _api сюда не подходит,
    а нужен он теперь дважды — задать папку и сменить логин с паролем.
    """

    def __init__(self) -> None:
        self.base = config.service_url(config.BY_KEY["qbittorrent"])
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def post(self, path: str, fields: dict):
        return self.opener.open(urllib.request.Request(
            f"{self.base}{path}", data=urllib.parse.urlencode(fields).encode(),
            headers={"Referer": self.base}), timeout=media.TIMEOUT)

    def preferences(self) -> dict:
        return json.loads(self.opener.open(f"{self.base}/api/v2/app/preferences",
                                           timeout=media.TIMEOUT).read())

    def login(self, password: str, username: str | None = None) -> bool:
        """Признак удачного входа — кука QBT_SID, а не тело ответа.

        qBittorrent 5.2+ отвечает на успех 204 No Content, и проверка по телу
        даёт ложный провал на верном пароле.
        """
        self.post("/api/v2/auth/login",
                  {"username": username or config.qbittorrent_login(),
                   "password": password})
        return any(c.name.startswith("QBT_SID") for c in self.jar)


# Минимальная длина взята у самого qBittorrent: короче он пароль не принимает.
MIN_PASSWORD = 6


def check_credentials(login: str, password: str) -> str:
    """Пустая строка значит «годится», иначе — что сказать человеку.

    Латиница обязательна, и это не вкусовщина, а измерение. С кириллическим
    логином или паролем qBittorrent пароль меняет и сам пускает, а Sonarr с
    Radarr получают от него 401 на /api/v2/app/webapiVersion и отвечают
    «Unable to connect to qBittorrent». То есть человек сменил бы пароль, увидел
    бы, что вход работает, — и заказы перестали бы скачиваться, без единой
    заметной ошибки. Проверено на живой установке, обе половины по отдельности.
    """
    if not login or not password:
        return "Заполните оба поля — логин и пароль."
    for value, what in ((login, "Логин"), (password, "Пароль")):
        if not value.isascii():
            return (f"{what} должен быть на латинице. Русские буквы qBittorrent "
                    "принимает, а Sonarr и Radarr после этого перестают к нему "
                    "подключаться — и заказы молча перестают скачиваться.")
        if value.strip() != value:
            return f"{what} не должен начинаться или заканчиваться пробелом."
    if len(password) < MIN_PASSWORD:
        return f"Пароль короче {MIN_PASSWORD} знаков qBittorrent не примет."
    return ""


def set_credentials(current_password: str, login: str, password: str) -> Step:
    """Меняет логин и пароль торрента на уже установленной системе.

    Через API, а не пересозданием Secret'а: конфиг засевается initContainer'ом
    только когда файла ещё нет, и на работающей установке новый Secret не изменил
    бы ничего — человек бы решил, что пароль сменился, а он бы остался прежним.

    Успех проверяется не кодом ответа, а входом с НОВЫМИ данными в чистой сессии:
    setPreferences отвечает 200 и на то, что не применилось.
    """
    title = "Логин и пароль торрента"
    problem = check_credentials(login, password)
    if problem:
        return Step(title, False, problem)
    try:
        torrent = _Torrent()
        if not torrent.login(current_password):
            return Step(title, False, "прежний пароль не подошёл — сменить не вышло")
        torrent.post("/api/v2/app/setPreferences", {"json": json.dumps(
            {"web_ui_username": login, "web_ui_password": password})})
        if not _Torrent().login(password, username=login):
            return Step(title, False, "торрент не принял новые данные")
    except (urllib.error.URLError, OSError, ValueError) as error:
        return Step(title, False, f"торрент не ответил: {error}")
    return Step(title, True, f"логин {login}, пароль сменён")


def set_download_dir(password: str) -> Step:
    """Говорит торренту, куда складывать скачанное.

    Сама она этого не знает: по умолчанию qBittorrent сохраняет в свой
    /config/Downloads — то есть на том с настройками, а не на том с медиатекой.
    Последствия молчаливые и дорогие. Том с настройками маленький, но хуже
    другое: жёсткая ссылка через границу томов невозможна, и *arr на импорте
    делают копию вместо неё. Каждый фильм занимает место дважды, ошибки при этом
    нет ни одной. Ровно ради совпадения томов downloads и library и лежат рядом.

    Ходим сюда своим клиентом, а не через _api: у торрента не ключ в заголовке, а
    сессия с кукой, и общий помощник для *arr сюда не подходит.
    """
    title = "Папка для скачивания в торренте"
    torrent = _Torrent()
    try:
        if not torrent.login(password):
            return Step(title, False, "торрент не принял пароль")
        if torrent.preferences().get("save_path", "").rstrip("/") == config.DOWNLOADS_DIR:
            return Step(title, True, f"уже {config.DOWNLOADS_DIR}")
        torrent.post("/api/v2/app/setPreferences",
                     {"json": json.dumps({"save_path": config.DOWNLOADS_DIR})})
        # Проверяем не код ответа на запись, а то, что торрент теперь говорит сам.
        saved = torrent.preferences().get("save_path", "").rstrip("/")
    except (urllib.error.URLError, OSError, ValueError) as error:
        return Step(title, False, f"торрент не ответил: {error}")
    if saved != config.DOWNLOADS_DIR:
        return Step(title, False, f"осталась {saved!r}")
    return Step(title, True, f"{config.DOWNLOADS_DIR} — на одном томе с библиотекой, "
                             f"иначе фильм занял бы место дважды")


def add_root_folder(key: str, api_key: str) -> Step:
    """Корневая папка — куда раскладывать разобранное."""
    path = config.ROOT_FOLDERS[key]
    title = f"Папка для готового в {config.BY_KEY[key].title}"
    _, existing, _ = _api(key, api_key, "/api/v3/rootfolder")
    if any(f.get("path", "").rstrip("/") == path for f in existing or []):
        return Step(title, True, f"уже задана: {path}")
    code, _, reason = _api(key, api_key, "/api/v3/rootfolder", data={"path": path})
    return (Step(title, True, path) if code in (200, 201)
            else _failed(title, code, reason))


def enable_hardlinks(key: str, api_key: str) -> Step:
    """Главная настройка ради экономии места.

    Без неё импорт КОПИРУЕТ файл из папки загрузок в библиотеку, и каждый фильм
    занимает место дважды. Ошибки при этом нет — просто вдвое меньше фильмов
    поместится на диск, и человек не поймёт почему.
    """
    title = f"Экономия места в {config.BY_KEY[key].title}"
    code, cfg, _ = _api(key, api_key, "/api/v3/config/mediamanagement")
    if not cfg:
        return Step(title, False, f"настройки не прочитались ({code})")
    if cfg.get("copyUsingHardlinks"):
        return Step(title, True, "уже включена")
    cfg["copyUsingHardlinks"] = True
    code, _, reason = _api(key, api_key,
                               f"/api/v3/config/mediamanagement/{cfg['id']}",
                               data=cfg, method="PUT")
    return (Step(title, True, "включена: фильм не занимает место дважды")
            if code in (200, 202) else _failed(title, code, reason))


def link_prowlarr(prowlarr_key: str, key: str, api_key: str) -> Step:
    """Prowlarr раздаёт свои индексаторы в Sonarr и Radarr.

    Настраивается один раз здесь, а не в каждом из них по отдельности — в этом и
    смысл Prowlarr. Добавили трекер у него — он появился у обоих.
    """
    service = config.BY_KEY[key]
    title = f"Поиск для {service.title}"
    _, existing, _ = _api("prowlarr", prowlarr_key, "/api/v1/applications")
    if any(a.get("name", "").lower() == key for a in existing or []):
        return Step(title, True, "уже связан")

    entry = _schema_entry("prowlarr", prowlarr_key, "/api/v1/applications/schema",
                          service.title)
    if not entry:
        return Step(title, False, f"Prowlarr не знает про {service.title}")

    entry["name"] = key
    entry["syncLevel"] = "fullSync"
    _set_field(entry, "prowlarrUrl", config.internal_url("prowlarr"))
    _set_field(entry, "baseUrl", config.internal_url(key))
    _set_field(entry, "apiKey", api_key)
    _set_field(entry, "syncCategories", SYNC_CATEGORIES[key])
    code, _, reason = _api("prowlarr", prowlarr_key, "/api/v1/applications",
                               data=entry)
    return (Step(title, True, "связан") if code in (200, 201)
            else _failed(title, code, reason))


# --- Всё вместе -------------------------------------------------------------

def configure(config_dir: Path | None = None, on_line=None) -> list[Step]:
    """Связывает сервисы между собой. Возвращает по шагу на каждое действие.

    Список, а не первое падение: если Prowlarr ещё не поднялся, это не повод не
    настраивать всё остальное — человек нажмёт кнопку ещё раз, и доделается только
    то, чего не хватило.
    """
    state = config.load_state()
    config_dir = config_dir or Path(state.get("config_dir")
                                    or config.default_config_dir())
    password = state.get("qbittorrent_password") or ""
    steps: list[Step] = []

    def log(step: Step) -> None:
        steps.append(step)
        if on_line:
            on_line(f"{'✓' if step.ok else '✕'} {step.title}: {step.detail}")

    keys = {}
    for service in ("sonarr", "radarr", "prowlarr"):
        key = wait_for_api_key(config_dir, service)
        keys[service] = key
        if not key:
            log(Step(f"Доступ к {config.BY_KEY[service].title}", False,
                     "сервис ещё не создал свой ключ — подождите пару минут "
                     "и нажмите кнопку снова"))

    # Куда качать — настройка одна на весь торрент, поэтому до цикла по сервисам.
    if password:
        log(set_download_dir(password))

    # Прокси тоже один на весь Prowlarr, а не по сервису. Сразу за ним — метки
    # на индексерах: без них прокси заведён, но не применяется ни к чему.
    if keys.get("prowlarr"):
        log(add_flaresolverr(keys["prowlarr"]))
        log(tag_indexers(keys["prowlarr"]))

    for service in ("sonarr", "radarr"):
        if not keys.get(service):
            continue
        if password:
            log(add_download_client(service, keys[service], password))
        else:
            log(Step(f"Торрент в {config.BY_KEY[service].title}", False,
                     "пароль торрента не найден — он создаётся при установке"))
        log(add_root_folder(service, keys[service]))
        log(enable_hardlinks(service, keys[service]))
        if keys.get("prowlarr"):
            log(link_prowlarr(keys["prowlarr"], service, keys[service]))
    return steps


def _self_check() -> None:
    """Проверяется то, где есть логика: подстановка полей в шаблон.

    Сами запросы к сервисам проверяются в e2e на живом кластере — здесь их
    подделывать бессмысленно, разъедется как раз то, что подделано.
    """
    entry = {"implementation": "QBittorrent",
             "fields": [{"name": "host", "value": "localhost"},
                        {"name": "port", "value": 1}]}
    _set_field(entry, "host", "qbittorrent")
    _set_field(entry, "password", "секрет")      # поля не было — должно добавиться
    values = {f["name"]: f["value"] for f in entry["fields"]}
    assert values["host"] == "qbittorrent"
    assert values["password"] == "секрет"

    # Правило про латиницу: ослабить его правкой легко, а сломается от этого не
    # торрент, а скачивание в Sonarr и Radarr — молча.
    assert check_credentials("admin", "Parol-77") == ""
    assert check_credentials("admin", "a&b=c%d+e!") == ""
    assert "латинице" in check_credentials("вася", "Parol-77")
    assert "латинице" in check_credentials("admin", "Пароль-77")
    assert "Заполните" in check_credentials("", "Parol-77")
    assert "короче" in check_credentials("admin", "abc")
    assert "пробелом" in check_credentials("admin", " Parol-77")
    assert values["port"] == 1, "лишние поля трогать нельзя"

    # Адреса для связок — только внутренние: снаружи их не видно из пода.
    assert config.internal_url("sonarr") == "http://sonarr:8989"
    assert config.internal_url("qbittorrent") == "http://qbittorrent:8080"
    assert config.ROOT_FOLDERS["sonarr"].startswith(config.DATA_MOUNT)
    assert set(SYNC_CATEGORIES) == {"sonarr", "radarr"}
    print("wire.py self-check: OK")


if __name__ == "__main__":
    _self_check()
