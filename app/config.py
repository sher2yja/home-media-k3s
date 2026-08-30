"""Единственный источник истины по портам, путям и человеческим названиям.

Стек разворачивается в двух формах: kubeadm-стенд на трёх виртуальных машинах (там
он разрабатывается) и k3s на машине пользователя. Главный риск в том, что формы
разъедутся: где-то Sonarr на 8989, где-то на 30989, в документации третье. Поэтому
таблица одна и лежит здесь, а манифесты и тексты приложения сверяются с ней —
проверкой в CI, а не обещанием быть внимательным.

Одинаковые порты в обеих формах — сознательное решение. «Родные» 8096/8989 читались
бы привычнее, но тогда у пользователя, документации и автора разные наборы адресов
на один и тот же продукт.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Service:
    key: str
    title: str          # как называть в интерфейсе для обычного человека
    port: int           # порт снаружи: одинаковый в k3s и на стенде
    internal_port: int  # порт внутри сети — по нему сервисы ходят друг к другу
    blurb: str          # что это, одной фразой без жаргона
    user_facing: bool   # показывать ли тому, кто просто хочет смотреть кино
    health_path: str = "/"


# Порядок важен: в таком виде список показывается пользователю.
SERVICES: tuple[Service, ...] = (
    Service(
        key="jellyfin",
        title="Jellyfin",
        port=30096,
        internal_port=8096,
        blurb="Здесь вы смотрите фильмы и сериалы. Главное окно всей системы.",
        user_facing=True,
        health_path="/health",
    ),
    Service(
        key="jellyseerr",
        title="Jellyseerr",
        port=30055,
        internal_port=5055,
        blurb="Здесь вы заказываете фильм или сериал, которого ещё нет.",
        user_facing=True,
        health_path="/api/v1/status",
    ),
    Service(
        key="sonarr",
        title="Sonarr",
        port=30989,
        internal_port=8989,
        blurb="Следит за сериалами: сам находит и качает новые серии.",
        user_facing=False,
    ),
    Service(
        key="radarr",
        title="Radarr",
        port=30878,
        internal_port=7878,
        blurb="То же самое, но для фильмов.",
        user_facing=False,
    ),
    Service(
        key="prowlarr",
        title="Prowlarr",
        port=30696,
        internal_port=9696,
        blurb="Список сайтов, где искать. Настраивается один раз.",
        user_facing=False,
    ),
    Service(
        key="qbittorrent",
        title="qBittorrent",
        port=30880,
        internal_port=8080,
        blurb="Качалка. Работает сама, заходить сюда обычно не нужно.",
        user_facing=False,
    ),
)

# Grafana идёт только в профиле «полный», поэтому она вне основного списка.
GRAFANA = Service(
    key="grafana",
    title="Grafana",
    port=30300,
    internal_port=3000,
    blurb="Графики: сколько занято памяти и места на диске.",
    user_facing=False,
    health_path="/api/health",
)

BY_KEY = {s.key: s for s in SERVICES + (GRAFANA,)}

# FlareSolverr стоит отдельно от таблицы выше, и это не небрежность. У всех
# сервисов в SERVICES есть NodePort, человек открывает их в браузере, и CI сверяет
# эти номера с манифестами. У FlareSolverr наружу не смотрит ничего: с ним
# разговаривает только Prowlarr, изнутри кластера, по k8s-DNS. Заводить ему
# NodePort значило бы открыть наружу порт, которым никто не пользуется, а класть
# его в SERVICES — сломать и сверку портов, и список сервисов в окне.
FLARESOLVERR_PORT = 8191
FLARESOLVERR_URL = f"http://flaresolverr:{FLARESOLVERR_PORT}/"


# --- Профили установки ------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    key: str
    title: str
    blurb: str
    with_monitoring: bool


PROFILES: tuple[Profile, ...] = (
    Profile(
        key="simple",
        title="Простой",
        blurb=(
            "Шесть сервисов и всё. Меньше всего лишнего, быстрее ставится, "
            "меньше ест памяти. Если сомневаетесь — берите этот."
        ),
        with_monitoring=False,
    ),
    Profile(
        key="full",
        title="Полный",
        blurb=(
            "То же самое плюс страница с графиками: сколько свободно места на "
            "диске и памяти, не пора ли чистить. Больше ничего не добавляет, "
            "фильмы качаются одинаково. Занимает ещё около 500 МБ памяти."
        ),
        with_monitoring=True,
    ),
)

PROFILE_BY_KEY = {p.key: p for p in PROFILES}


# --- Пути внутри контейнеров ------------------------------------------------

# У четырёх сервисов /data должен быть БУКВАЛЬНО одинаковым. Это не стиль, а
# условие работы hardlink'ов: Sonarr и Radarr делают на импорте жёсткую ссылку
# вместо копии, только если видят файл по тому же пути на той же файловой системе.
# Иначе каждый фильм занимает место дважды. См. docs/architecture.md.
DATA_MOUNT = "/data"
MEDIA_SUBDIRS = ("downloads", "library/tv", "library/movies")

# Куда Sonarr и Radarr складывают разобранное. Пути ВНУТРИ контейнера, поэтому от
# того, какую папку выбрал человек снаружи, они не зависят: снаружи меняется только
# то, что смонтировано в /data. Глубина важна — это папка НАД отдельными фильмами
# и сериалами, а не сам фильм.
ROOT_FOLDERS = {
    "sonarr": f"{DATA_MOUNT}/library/tv",
    "radarr": f"{DATA_MOUNT}/library/movies",
}
DOWNLOADS_DIR = f"{DATA_MOUNT}/downloads"


def internal_url(key: str) -> str:
    """Адрес, по которому сервисы ходят друг к другу внутри кластера.

    Не путать с service_url(): та даёт адрес для человека и браузера, снаружи и
    по NodePort. Внутри кластера работает k8s-DNS и РОДНЫЕ порты — если подставить
    сюда внешний порт, Sonarr не найдёт качалку, а Prowlarr — Sonarr.
    """
    service = BY_KEY[key]
    return f"http://{key}:{service.internal_port}"


# --- Пути на машине пользователя --------------------------------------------

APP_DIRNAME = "home-media-k3s"


def default_config_dir() -> Path:
    """Куда класть настройки сервисов (их базы, ключи, история)."""
    return Path.home() / ".local" / "share" / APP_DIRNAME / "config"


def default_media_dir() -> Path:
    """Куда складывать сами фильмы. Отдельно от настроек: этот каталог большой,
    его логично держать на другом диске, и пользователь его меняет чаще всего."""
    videos = Path.home() / "Videos"
    parent = videos if videos.is_dir() else Path.home()
    return parent / APP_DIRNAME


def service_url(service: Service, host: str = "localhost") -> str:
    return f"http://{host}:{service.port}"


# --- Состояние установки ----------------------------------------------------

# Что установщик должен помнить между запусками: какой профиль выбран, куда всё
# положено и какой пароль он сгенерировал qBittorrent. Пароль показывается один раз
# при установке — без этого файла человек, закрывший вкладку, теряет его насовсем.
#
# SHORTCUT: пароль лежит открытым текстом в файле пользователя. См. docs/shortcuts.md §11.


def state_path() -> Path:
    return default_config_dir().parent / "state.json"


def load_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_state(**values) -> dict:
    """Дописывает поля, а не перезаписывает файл: повторная установка не должна
    терять пароль, сгенерированный в прошлый раз."""
    state = load_state() | values
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    return state


def installed() -> bool:
    return bool(load_state().get("profile"))
