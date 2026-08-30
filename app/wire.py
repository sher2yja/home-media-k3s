"""Связывание сервисов между собой через их API.

То, что человек иначе делает руками полчаса: сказать Prowlarr про Sonarr и Radarr,
показать обоим качалку с паролем, задать корневые папки и включить hardlink'и.
Каждый шаг — несколько кликов в чужом интерфейсе с копированием ключей API из
одного окна в другое, и каждый можно молча сделать не так.

Ключи API нигде не спрашиваются: *arr держат их в своих config.xml, и эти файлы
лежат в папке настроек на этой же машине. Читает их media.arr_api_key.

Три принципа, от которых тут нельзя отступать:

1. **Идемпотентность.** Кнопку нажмут дважды. Каждый шаг сначала смотрит, нет ли
   уже такой записи, и только потом создаёт. Иначе в Sonarr появятся три качалки.
2. **Схему просим у сервиса, а не выдумываем.** У Sonarr, Radarr и Prowlarr поля
   настроек описаны в /schema, и набор полей меняется между версиями. Захардкоженный
   набор однажды разъедется с сервисом и даст ошибку, в которой не будет ни слова
   о причине.
3. **Внутренние адреса, а не внешние.** Сервисы ходят друг к другу по k8s-DNS и
   родным портам (http://sonarr:8989). Подставить сюда localhost:30989 —
   работающий с виду адрес, который не найдётся изнутри пода.
"""

from __future__ import annotations

import json
import time
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
    """Качалка в Sonarr или Radarr. Без неё они находят раздачу и не могут её взять."""
    title = f"Качалка в {config.BY_KEY[key].title}"
    _, existing, _ = _api(key, api_key, "/api/v3/downloadclient")
    if any(c.get("name") == "qBittorrent" for c in existing or []):
        return Step(title, True, "уже прописана")

    entry = _schema_entry(key, api_key, "/api/v3/downloadclient/schema", "QBittorrent")
    if not entry:
        return Step(title, False, "сервис не предложил настроек для qBittorrent")

    entry["name"] = "qBittorrent"
    entry["enable"] = True
    qbt = config.BY_KEY["qbittorrent"]
    _set_field(entry, "host", "qbittorrent")     # k8s-DNS, а не localhost
    _set_field(entry, "port", qbt.internal_port)
    _set_field(entry, "username", "admin")
    _set_field(entry, "password", password)
    code, _, reason = _api(key, api_key, "/api/v3/downloadclient", data=entry)
    return (Step(title, True, "прописана") if code in (200, 201)
            else _failed(title, code, reason))


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

    for service in ("sonarr", "radarr"):
        if not keys.get(service):
            continue
        if password:
            log(add_download_client(service, keys[service], password))
        else:
            log(Step(f"Качалка в {config.BY_KEY[service].title}", False,
                     "пароль качалки не найден — он создаётся при установке"))
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
    assert values["port"] == 1, "лишние поля трогать нельзя"

    # Адреса для связок — только внутренние: снаружи их не видно из пода.
    assert config.internal_url("sonarr") == "http://sonarr:8989"
    assert config.internal_url("qbittorrent") == "http://qbittorrent:8080"
    assert config.ROOT_FOLDERS["sonarr"].startswith(config.DATA_MOUNT)
    assert set(SYNC_CATEGORIES) == {"sonarr", "radarr"}
    print("wire.py self-check: OK")


if __name__ == "__main__":
    _self_check()
