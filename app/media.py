"""Опрос шести сервисов и сведение их ответов в человеческие фразы.

Только стандартная библиотека: urllib вместо httpx. Запросы синхронные и мелкие,
FastAPI выполняет обычные def-обработчики в пуле потоков — асинхронный клиент тут
не даёт ничего, кроме лишней зависимости в сборке PyInstaller.

Ключи API нигде не спрашиваются у пользователя: *arr держат их в своих config.xml,
Jellyseerr — в settings.json, и оба каталога лежат на этой же машине. Просить
человека найти и скопировать ключ — ровно то, ради устранения чего писалось
приложение.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import config

TIMEOUT = 5


def _request(url: str, *, headers: dict | None = None, data: bytes | None = None,
             method: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:  # noqa: BLE001
        # Ловим всё намеренно. Эта функция обслуживает страницу статуса: любая
        # ошибка сети, таймаут или мусор в ответе обязаны превратиться в "сервис
        # не отвечает", а не в пятисотку у человека, который просто хотел
        # посмотреть, работает ли Jellyfin.
        return 0, b""


def _json(url: str, **kw):
    code, body = _request(url, **kw)
    if code != 200 or not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


# --- Ключи API --------------------------------------------------------------

def arr_api_key(config_dir: Path, service: str) -> str | None:
    """Sonarr, Radarr и Prowlarr хранят ключ в <ApiKey> внутри config.xml."""
    path = config_dir / service / "config.xml"
    try:
        return ET.parse(path).getroot().findtext("ApiKey")
    except (OSError, ET.ParseError):
        return None


def jellyseerr_api_key(config_dir: Path) -> str | None:
    try:
        return json.loads((config_dir / "jellyseerr" / "settings.json").read_text()) \
            .get("main", {}).get("apiKey")
    except (OSError, ValueError):
        return None


# --- Живы ли сервисы --------------------------------------------------------

# 401/403 значит «отвечает, но требует входа» — сервис жив, это не поломка.
_ALIVE = (200, 204, 301, 302, 401, 403)


def service_alive(service, host: str = "localhost") -> bool:
    """Отвечает ли конкретный сервис. Отдельно от service_status, потому что
    предпроверка портов спрашивает про Grafana поимённо: в список сервисов та
    попадает по профилю из состояния, а порт занимает по факту работы."""
    code, _ = _request(f"http://{host}:{service.port}{service.health_path}")
    return code in _ALIVE


def service_status(host: str = "localhost") -> list[dict]:
    """Статус для показа человеку. Намеренно не «pod Running» и не код ответа:
    вопрос, на который отвечает эта страница, — «работает или нет»."""
    out = []
    state = config.load_state()
    services = list(config.SERVICES)
    profile = config.PROFILE_BY_KEY.get(state.get("profile", ""))
    # Grafana в списке, если её ВЫБРАЛИ или если она РАБОТАЕТ. Второе — не
    # перестраховка: профиль в состоянии говорит про выбор, а не про то, что
    # развёрнуто. Стоит человеку выбрать простой профиль и на вопрос «удалить
    # графики?» ответить «оставить» — графики работают, а кнопки на них нет, и
    # пропала она у того, кто сам попросил их оставить. Обратное тоже полезно:
    # профиль полный, Grafana не отвечает — строка покажет крестик, а не исчезнет.
    if (profile and profile.with_monitoring) or service_alive(config.GRAFANA, host):
        services.append(config.GRAFANA)
    for s in services:
        alive = service_alive(s, host)
        out.append({
            "key": s.key,
            "title": s.title,
            "blurb": s.blurb,
            # У Grafana адрес особый: главная у неё пустая, и кнопка обязана
            # вести сразу на готовый дашборд. Иначе человек, включивший полный
            # профиль ради графиков, попадает на страницу без единого графика.
            "url": (config.grafana_dashboard_url(host) if s is config.GRAFANA
                    else config.service_url(s, host)),
            "user_facing": s.user_facing,
            "alive": alive,
            "text": "работает" if alive else "не отвечает",
        })
    return out


# --- Вход по аккаунту Jellyfin ----------------------------------------------

# Заголовок обязателен, без него Jellyfin отвечает 400 на любой запрос авторизации.
_JF_AUTH_HEADER = ('MediaBrowser Client="my-pet", Device="installer", '
                   'DeviceId="my-pet-app", Version="1.0.0"')


def jellyfin_login(username: str, password: str, host: str = "localhost") -> dict | None:
    """Приложение работает от аккаунта самого зрителя, а не от админского.
    Поэтому чужие заказы ему не видны, и админку Jellyfin открывать не нужно."""
    url = f"http://{host}:{config.BY_KEY['jellyfin'].port}/Users/AuthenticateByName"
    body = json.dumps({"Username": username, "Pw": password}).encode()
    data = _json(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": _JF_AUTH_HEADER,
    })
    if not data:
        return None
    return {"token": data.get("AccessToken"),
            "user_id": data.get("User", {}).get("Id"),
            "name": data.get("User", {}).get("Name")}


# --- «Где мой фильм» --------------------------------------------------------

def _humanize_queue_item(item: dict) -> str:
    """Одна строка про то, что происходит с конкретным фильмом или серией."""
    size = item.get("size") or 0
    left = item.get("sizeleft")
    state = (item.get("trackedDownloadState") or "").lower()
    status = (item.get("status") or "").lower()

    if state in ("importpending", "importing") or status == "completed":
        return "скачано, раскладываем по местам — вот-вот появится"
    if status in ("warning", "failed"):
        return "что-то пошло не так при скачивании, система пробует ещё раз"
    if status == "delay" or state == "delay":
        return "ждём подходящую раздачу"
    if size and left is not None:
        percent = int((size - left) / size * 100)
        eta = item.get("timeleft")
        tail = f", осталось примерно {_humanize_eta(eta)}" if eta else ""
        return f"качается, {percent}%{tail}"
    return "качается"


def _humanize_eta(timeleft: str | None) -> str:
    """Sonarr и Radarr отдают остаток как 00:20:15 или 1.00:20:15."""
    if not timeleft:
        return ""
    days, _, clock = timeleft.rpartition(".")
    parts = clock.split(":")
    if len(parts) != 3:
        return timeleft
    h, m = int(parts[0]), int(parts[1])
    if days and int(days) > 0:
        return f"{int(days)} дн."
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин"
    return "меньше минуты"


# Статусы медиа в Jellyseerr — числа. Расшифровка из его API.
_SEERR_MEDIA = {
    1: "заказ отправлен, ждём",
    2: "заказ ждёт подтверждения",
    3: "ищем и качаем",
    4: "часть серий уже доступна",
    5: "готово, можно смотреть",
}


def where_is_my_movie(config_dir: Path, host: str = "localhost",
                      jellyfin_user_id: str | None = None) -> list[dict]:
    """Сводит очереди Sonarr и Radarr с заказами Jellyseerr.

    Очередь *arr — главный источник: только в ней есть проценты. Заказы Jellyseerr
    добавляют то, до чего очередь ещё не дошла, иначе только что заказанный фильм
    просто не появился бы в списке и это выглядело бы как потеря заказа.

    jellyfin_user_id — показывать только заказы этого человека. Очередь *arr при
    этом не фильтруется: она общая и авторства заказа не знает.
    """
    items: list[dict] = []
    seen: set[str] = set()

    for svc, path in (("sonarr", "/api/v3/queue"), ("radarr", "/api/v3/queue")):
        key = arr_api_key(config_dir, svc)
        if not key:
            continue
        port = config.BY_KEY[svc].port
        data = _json(f"http://{host}:{port}{path}?pageSize=100",
                     headers={"X-Api-Key": key})
        for rec in (data or {}).get("records", []):
            title = rec.get("title") or "Без названия"
            items.append({"title": title, "status": _humanize_queue_item(rec),
                          "source": svc, "done": False})
            seen.add(title.lower())

    key = jellyseerr_api_key(config_dir)
    if key:
        port = config.BY_KEY["jellyseerr"].port
        data = _json(f"http://{host}:{port}/api/v1/request?take=50&sort=added",
                     headers={"X-Api-Key": key})
        for rec in (data or {}).get("results", []):
            if jellyfin_user_id:
                by = rec.get("requestedBy", {}) or {}
                if by.get("jellyfinUserId") != jellyfin_user_id:
                    continue
            media = rec.get("media", {}) or {}
            title = (media.get("title")
                     or (rec.get("media", {}) or {}).get("originalTitle")
                     or f"Заказ #{rec.get('id')}")
            if title.lower() in seen:
                continue
            status = _SEERR_MEDIA.get(media.get("status"), "заказ принят")
            items.append({"title": title, "status": status,
                          "source": "jellyseerr",
                          "done": media.get("status") == 5})
    return items


def _self_check() -> None:
    """Минимальная проверка разбора — единственное место с настоящей логикой."""
    assert _humanize_eta("00:20:15") == "20 мин"
    assert _humanize_eta("01:05:00") == "1 ч 5 мин"
    assert _humanize_eta("2.03:00:00") == "2 дн."
    assert _humanize_eta("00:00:30") == "меньше минуты"
    assert _humanize_eta(None) == ""
    assert _humanize_queue_item({"size": 100, "sizeleft": 25}) == "качается, 75%"
    assert "осталось примерно 10 мин" in _humanize_queue_item(
        {"size": 100, "sizeleft": 50, "timeleft": "00:10:00"})
    assert _humanize_queue_item({"trackedDownloadState": "importPending"}) \
        .startswith("скачано")
    assert _humanize_queue_item({"status": "failed"}).startswith("что-то пошло не так")
    # Нет данных о размере — не должно падать и не должно врать про проценты.
    assert _humanize_queue_item({}) == "качается"
    print("media.py self-check: OK")


if __name__ == "__main__":
    _self_check()
