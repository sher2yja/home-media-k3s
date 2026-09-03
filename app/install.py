"""Установка стека на машину пользователя: кластер k3s, тома, пароль qBittorrent.

Приложение ставит медиасервер только на Linux: k3s ложится в систему службой, а
`kubectl` берётся из самого k3s. Ветвлений по ОС здесь нет ни одного — путь через
WSL2 снят 2026-08-30, потому что проверить его было нечем (машины с Windows нет).
Он вернётся отдельной работой; снятый код целиком лежит в теге v0.1.3.

Здесь же лежит ЕДИНСТВЕННАЯ в проекте реализация PBKDF2-хеша для qBittorrent.
Её же вызывает infra/vms/scripts/08-qbittorrent-secret.sh, который делает то же
самое для стенда. Две копии одной криптографии — способ однажды тихо разъехаться
и получить «неверный пароль» без объяснений.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import config
import icon
import media
import wire

# --- Где лежат манифесты ----------------------------------------------------

def bundle_root() -> Path:
    """PyInstaller распаковывает данные во временный каталог и кладёт путь в
    sys._MEIPASS. При запуске из репозитория корень — каталог выше app/."""
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent.parent


def overlay_dir() -> Path:
    """Оверлей kustomize. Внутри он ссылается на ../../k8s/media-stack, поэтому в
    бандл обязаны попасть оба каталога — см. datas в home-media-k3s.spec."""
    return bundle_root() / "deploy" / "k3s"


def monitoring_dir() -> Path:
    # Оверлей, а не k8s/monitoring напрямую. База несёт nodeSelector на узел
    # стенда, и без патча Prometheus с Grafana вставали в Pending навсегда —
    # молча, потому что apply при этом отрабатывает успешно.
    return bundle_root() / "deploy" / "k3s" / "monitoring"


# --- Пароль qBittorrent -----------------------------------------------------

# qBittorrent хранит пароль как PBKDF2-HMAC-SHA512: 100000 итераций, соль 16 байт,
# выход 64 байта, обе части в base64 через двоеточие внутри @ByteArray(...).
# Параметры заданы самим qBittorrent, менять их нельзя.
PBKDF2_ITERATIONS = 100_000
PBKDF2_SALT_BYTES = 16
PBKDF2_KEY_BYTES = 64


def generate_password(length: int = 20) -> str:
    """Пароль рождается при установке, а не лежит в репозитории: иначе он был бы
    один на всех, кто скачал релиз. Только буквы и цифры — этот пароль человек
    будет вводить руками в Sonarr и Radarr."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def qbittorrent_pbkdf2(password: str) -> str:
    salt = os.urandom(PBKDF2_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha512", password.encode(), salt, PBKDF2_ITERATIONS, dklen=PBKDF2_KEY_BYTES
    )
    return (f"@ByteArray({base64.b64encode(salt).decode()}"
            f":{base64.b64encode(dk).decode()})")


# --- Слой запуска команд ----------------------------------------------------

class ManualStepRequired(Exception):
    """Шаг требует прав, которых приложение не получило. Несёт готовую команду —
    человеку остаётся скопировать её, а не сочинять самому."""

    def __init__(self, message: str, command: str) -> None:
        super().__init__(message)
        self.command = command


def run(cmd: list[str], *, timeout: int = 1800,
        on_line=None) -> subprocess.CompletedProcess:
    """Запускает команду, отдавая вывод построчно в on_line.

    Построчно, а не одним куском в конце: установка идёт минуты, и окно, в котором
    ничего не происходит, человек закрывает. Ошибки не глотаем и returncode
    разбираем сами — при разборе проблемы нужен сырой текст, а не наш пересказ.

    errors="replace", а не строгое декодирование: наружу печатает чужой установщик
    k3s, и одна битая последовательность в его выводе не должна ронять установку.
    """
    if on_line:
        on_line("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        lines.append(line)
        if on_line:
            on_line(line)
        if time.monotonic() > deadline:
            proc.kill()
            lines.append(f"Прервано по таймауту ({timeout} c)")
            break
    proc.wait()
    return subprocess.CompletedProcess(cmd, proc.returncode, "\n".join(lines), "")


def kubectl_command(*args: str) -> list[str]:
    """kubectl отдельно не ставится: k3s несёт его в себе, а kustomize встроен
    в сам kubectl. Одной зависимостью меньше и на диске, и в бандле."""
    return ["k3s", "kubectl", *args]


def kubectl(*args: str, on_line=None, timeout: int = 900) -> subprocess.CompletedProcess:
    return run(kubectl_command(*args), on_line=on_line, timeout=timeout)


# --- Подъём кластера --------------------------------------------------------

K3S_INSTALL_URL = "https://get.k3s.io"
# Чем программа представляется в сети. См. fetch_installer.
USER_AGENT = "home-media-k3s (+https://github.com/sher2yja/home-media-k3s)"

# Kubeconfig по умолчанию доступен только root. Приложение работает от обычного
# пользователя, и без этого флага каждый вызов kubectl требовал бы повышения прав.
# SHORTCUT: kubeconfig становится читаемым всем локальным пользователям, а это
# полный доступ к кластеру. Для домашней машины принято сознательно.
# --disable=traefik: k3s ставит Traefik ingress-контроллером, а в этом проекте нет
# ни одного Ingress — все сервисы выведены наружу через NodePort, и Traefik не
# обслуживает ничего. Зато его LoadBalancer забирает ДВА СЛУЧАЙНЫХ порта из
# 30000-32767, то есть из того же диапазона, где стоят наши семь фиксированных.
# Разворачивается он при первом старте кластера, раньше них: при совпадении наш
# Service не создаётся, а предпроверка сообщает о занятом порте, которого человек
# нигде не выбирал и в настройках не найдёт. Один раз это уже стоило полного
# профиля целиком (история — у GRAFANA в config.py). Возвращается снятием флага.
K3S_EXEC = "--write-kubeconfig-mode 0644 --disable=traefik"


def k3s_installed() -> bool:
    return shutil.which("k3s") is not None


def fetch_installer() -> bytes:
    """Забирает скрипт установки k3s.

    Заголовок User-Agent обязателен. По умолчанию urllib представляется как
    `Python-urllib/3.12`, и CDN за get.k3s.io отвечает на это `403 Forbidden` —
    установка падала у каждого пользователя Linux на первом же шаге, а в окне
    было написано только «HTTP Error 403: Forbidden». Представляемся честно:
    подделывать чужой User-Agent, чтобы обойти чужое правило, незачем — хватает
    любого своего.
    """
    request = urllib.request.Request(K3S_INSTALL_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as r:
        return r.read()


def _download_installer() -> Path:
    """Скачиваем скрипт в файл, а не запускаем `curl | sh`. Разница не
    косметическая: скачанный файл можно показать человеку и он же остаётся на
    диске, если установка упала и нужно разбираться."""
    path = Path(tempfile.gettempdir()) / "k3s-install.sh"
    try:
        path.write_bytes(fetch_installer())
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Не удалось скачать установщик k3s с {K3S_INSTALL_URL}: {e}. "
            f"Проверьте интернет и попробуйте ещё раз."
        ) from e
    return path


def install_k3s_linux(on_line=None) -> None:
    """pkexec, а не sudo: у приложения нет терминала, чтобы спросить пароль.
    pkexec показывает системное окно ввода — то самое, к которому человек привык."""
    script = _download_installer()
    manual = f'curl -sfL {K3S_INSTALL_URL} | INSTALL_K3S_EXEC="{K3S_EXEC}" sh -'
    if not shutil.which("pkexec"):
        raise ManualStepRequired(
            "Не нашёл pkexec — окно запроса пароля показать нечем.", manual)
    # pkexec чистит окружение, поэтому переменная передаётся через env, а не
    # экспортом перед командой: экспорт до неё просто не доедет.
    r = run(["pkexec", "env", f"INSTALL_K3S_EXEC={K3S_EXEC}", "sh", str(script)],
            on_line=on_line)
    if r.returncode == 126:   # человек закрыл окно ввода пароля или нажал «отмена»
        raise ManualStepRequired("Установка отменена: пароль не введён.", manual)
    if r.returncode != 0:
        raise RuntimeError(f"Установщик k3s завершился с кодом {r.returncode}")


def ensure_cluster(on_line=None) -> None:
    """Шов, через который однажды вернётся вторая ОС: сценарий установки выше по
    коду не знает, чем именно поднят кластер, и от способа не зависит."""
    if not k3s_installed():
        install_k3s_linux(on_line)


NODE_POLL = 2   # как часто спрашивать, появился ли узел


def wait_for_node(timeout: int = 300, on_line=None) -> None:
    """Сразу после установки kubelet ещё регистрируется, и apply падает на
    «no such host». Ждём готовности ноды, а не спим фиксированное время.

    Двумя шагами, и это не перестраховка. `kubectl wait --all` по ресурсу,
    которого ЕЩЁ НЕТ, не ждёт вовсе: он мгновенно выходит с «no matching
    resources found». А сразу после установки Node как раз не зарегистрирован.
    Одношаговая проверка превращала обычную медлительность машины в «Кластер не
    пришёл в готовность» через доли секунды после успешно поставленного k3s —
    сообщение, по которому человеку нечего делать. Ловилось это только
    случайными падениями e2e, и один раз упало на коммите, который этого кода
    не касался вовсе.
    """
    if on_line:
        on_line("Жду, пока кластер станет готов")
    deadline = time.monotonic() + timeout
    while True:
        r = kubectl("get", "nodes", "-o", "name", timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Кластер не поднялся: узел не зарегистрировался за {timeout} с. "
                + r.stdout[-2000:])
        time.sleep(NODE_POLL)
    # Остаток времени отдаём ожиданию готовности, но не меньше полминуты: узел
    # только что появился, и на condition=Ready ему нужно ещё немного.
    left = max(30, int(deadline - time.monotonic()))
    r = kubectl("wait", "--for=condition=Ready", "node", "--all",
                f"--timeout={left}s", on_line=on_line, timeout=left + 60)
    if r.returncode != 0:
        raise RuntimeError("Кластер не пришёл в готовность: " + r.stdout[-2000:])


# --- Каталоги и тома --------------------------------------------------------

def prepare_dirs(config_dir: Path, media_dir: Path) -> None:
    """Каталоги создаём заранее и сами, от имени пользователя. Если их создаст
    kubelet при первом монтировании, владельцем станет root, и контейнеры под
    PUID=1000 не смогут туда писать — ошибка при этом будет невнятной. Поэтому же
    тома описаны как type: Directory, а не DirectoryOrCreate."""
    for service in config.SERVICES:
        (config_dir / service.key).mkdir(parents=True, exist_ok=True)
    # downloads и library — на ОДНОМ томе: hardlink через границу файловых систем
    # невозможен, и *arr молча свалятся обратно на копирование.
    for sub in config.MEDIA_SUBDIRS:
        (media_dir / sub).mkdir(parents=True, exist_ok=True)


def render_volumes(config_dir: Path, media_dir: Path) -> str:
    """Подставляет выбранные пользователем пути в шаблон томов.

    Пути прогоняются через json.dumps: YAML — надмножество JSON, поэтому его
    кавычки и экранирование здесь корректны, и пробел или кириллица в имени папки
    не ломают манифест. PyYAML ради этого в зависимости не тянем.
    """
    tmpl = (overlay_dir() / "hostpath-volumes.yaml.tmpl").read_text(encoding="utf-8")
    values = {"media": json.dumps(str(media_dir))}
    for service in config.SERVICES:
        values[service.key] = json.dumps(str(config_dir / service.key))
    return tmpl.format(**values)


def apply_volumes(config_dir: Path, media_dir: Path, on_line=None) -> None:
    rendered = render_volumes(config_dir, media_dir)
    path = Path(tempfile.gettempdir()) / "home-media-k3s-volumes.yaml"
    path.write_text(rendered, encoding="utf-8")
    r = kubectl("apply", "-f", str(path), on_line=on_line)
    if r.returncode != 0:
        raise RuntimeError("Не удалось создать тома: " + r.stdout[-2000:])


# --- Пункт меню приложений (Linux) ------------------------------------------

DESKTOP_ID = "home-media-k3s"

# Имя класса окна должно совпадать с ui.WM_CLASS_NAME: по нему GNOME связывает
# открытое окно с этим файлом. Не совпадёт — окно останется безымянным с серой
# заглушкой вместо иконки.
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Домашний медиасервер
Comment=Установка и управление домашним медиасервером
Exec={command}
Icon={icon}
Terminal=false
Categories=AudioVideo;Network;
StartupWMClass={wmclass}
"""


def _launch_command() -> str:
    """Команда запуска для пункта меню.

    В собранном бандле sys.executable — это сам бандл, и его достаточно. При
    запуске из исходников это интерпретатор, поэтому дописывается путь к main.py.
    """
    if getattr(sys, "frozen", False):
        return shlex.quote(sys.executable)
    main = Path(__file__).resolve().parent / "main.py"
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(main))}"


def ensure_desktop_entry() -> Path | None:
    """Прописывает приложение в меню и в панель задач. Возвращает путь к файлу.

    Зачем это вообще нужно, хотя иконка у окна уже есть: GNOME Shell 46 больше НЕ
    берёт картинку из свойства окна _NET_WM_ICON. Окну, которому не нашлось
    .desktop, он рисует стандартную заглушку application-x-executable — серую
    шестерёнку без имени. Проверено живьём на Ubuntu 24.04. Единственный способ
    показать своё — положить PNG на диск и сослаться на него отсюда.

    Побочная и не менее важная польза: приложение появляется в меню, и человеку
    больше не нужно каждый раз искать скачанный файл в «Загрузках».

    Ошибки глотаются намеренно: пункт меню — удобство, и если домашний каталог
    оказался недоступен на запись, установка медиасервера из-за этого падать
    не должна.
    """
    try:
        icons = Path.home() / ".local/share/icons/hicolor/64x64/apps"
        icons.mkdir(parents=True, exist_ok=True)
        (icons / f"{DESKTOP_ID}.png").write_bytes(icon.png_bytes())

        apps = Path.home() / ".local/share/applications"
        apps.mkdir(parents=True, exist_ok=True)
        entry = apps / f"{DESKTOP_ID}.desktop"
        entry.write_text(DESKTOP_ENTRY.format(
            command=_launch_command(), icon=DESKTOP_ID, wmclass=DESKTOP_ID),
            encoding="utf-8")
        entry.chmod(0o644)

        # Кэш меню обновляем, если есть чем: без этого пункт появляется не сразу.
        # Отсутствие утилиты — не ошибка, GNOME подхватит файл и сам.
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", str(apps)],
                           capture_output=True, check=False, timeout=30)
        return entry
    except OSError:
        return None


# --- Пароль qBittorrent в кластере ------------------------------------------

QBT_SECRET = "qbittorrent-webui"


def sync_qbittorrent_config(config_dir: Path, login: str, password: str) -> bool:
    """Синхронизирует существующий конфиг с только что созданным Secret.

    Init-контейнер создаёт файл с нуля, но намеренно не перезаписывает прежний.
    После неудачной установки такой файл уже есть, а Secret ещё нет: без этой
    синхронизации новый пароль показывается человеку, но qBittorrent его не знает.
    """
    path = config_dir / "qbittorrent" / "qBittorrent" / "qBittorrent.conf"
    if not path.exists():
        return False

    values = {
        "WebUI\\Username": login,
        "WebUI\\Password_PBKDF2": f'"{qbittorrent_pbkdf2(password)}"',
    }
    lines = path.read_text(encoding="utf-8").splitlines()
    found: set[str] = set()
    for index, line in enumerate(lines):
        for key, value in values.items():
            if line.startswith(key + "="):
                lines[index] = f"{key}={value}"
                found.add(key)

    missing = [f"{key}={value}" for key, value in values.items() if key not in found]
    if missing:
        try:
            insert_at = lines.index("[Preferences]") + 1
        except ValueError:
            lines.extend(["", "[Preferences]"])
            insert_at = len(lines)
        lines[insert_at:insert_at] = missing

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.chmod(path.stat().st_mode)
    temporary.replace(path)
    return True


def prepare_qbittorrent_config(config_dir: Path, login: str, password: str,
                               on_line=None) -> bool:
    """Останавливает прежний pod до синхронизации, чтобы тот не вернул старый хеш."""
    path = config_dir / "qbittorrent" / "qBittorrent" / "qBittorrent.conf"
    if not path.exists():
        return False
    kubectl("scale", "deployment/qbittorrent", "-n", "media", "--replicas=0",
            on_line=on_line, timeout=180)
    kubectl("wait", "--for=delete", "pod",
            "-l", "app.kubernetes.io/name=qbittorrent", "-n", "media",
            "--timeout=180s", on_line=on_line, timeout=240)
    return sync_qbittorrent_config(config_dir, login, password)


def create_qbittorrent_secret(on_line=None, login: str = "",
                              password: str = "") -> str | None:
    """Возвращает новый пароль либо None, если Secret уже был.

    Идемпотентность здесь не про аккуратность: пароль сохранён в download client
    Sonarr и Radarr, и молчаливая смена сломала бы им скачивание. Дальше конфиг
    qBittorrent засевает initContainer seed-webui-config, читая хеш отсюда.

    login и password — то, что человек вписал сам при установке. Пустые значит
    «придумай сама», как было всегда. Пустой пароль сюда не попадает: поле в окне
    либо заполнено, либо не заполнено, а полупустое состояние ловится там же.
    """
    if kubectl("get", "secret", "-n", "media", QBT_SECRET, timeout=120).returncode == 0:
        return None
    password = password or generate_password()
    r = kubectl("create", "secret", "generic", QBT_SECRET, "-n", "media",
                f"--from-literal=login={login or config.QBT_DEFAULT_LOGIN}",
                f"--from-literal=password={password}",
                f"--from-literal=password-pbkdf2={qbittorrent_pbkdf2(password)}",
                on_line=on_line, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("Не удалось создать пароль торрента: " + r.stdout[-2000:])
    return password


def change_qbittorrent_credentials(login: str, password: str,
                                   on_line=None) -> list[wire.Step]:
    """Смена логина и пароля торрента на работающей установке.

    Три места, и все три обязательны — пропустить любое значит сломать
    скачивание молча:

      1. сам торрент, через его API (Secret тут бесполезен: конфиг засевается
         только при первом запуске, когда файла ещё нет);
      2. Secret в кластере — на случай, если том с настройками когда-нибудь
         пересоздадут: засев возьмёт данные оттуда, и они обязаны быть теми же;
      3. Sonarr и Radarr — они ходят в торрент со своим сохранённым паролем, и со
         старым просто перестают отдавать раздачи, без единой ошибки.

    Порядок именно такой. Сначала меняем там, где это может не выйти, и только
    потом записываем в состояние: иначе state.json обещал бы пароль, которым
    никуда не войти.
    """
    state = config.load_state()
    steps = [wire.set_credentials(state.get("qbittorrent_password", ""),
                                  login, password)]
    if on_line:
        on_line(f"{'✓' if steps[0].ok else '✕'} {steps[0].title}: {steps[0].detail}")
    if not steps[0].ok:
        return steps

    update_qbittorrent_secret(login, password, on_line)
    config.save_state(qbittorrent_login=login, qbittorrent_password=password)

    config_dir = Path(state.get("config_dir") or config.default_config_dir())
    for service in ("sonarr", "radarr"):
        key = media.arr_api_key(config_dir, service)
        step = (wire.update_download_client(service, key, password) if key
                else wire.Step(f"Торрент в {config.BY_KEY[service].title}", False,
                               "ключ API не прочитался"))
        steps.append(step)
        if on_line:
            on_line(f"{'✓' if step.ok else '✕'} {step.title}: {step.detail}")
    return steps


def update_qbittorrent_secret(login: str, password: str,
                              on_line=None) -> subprocess.CompletedProcess:
    """Перезаписывает Secret одним patch'ем.

    Не delete+create: между удалением и созданием под, если он в этот момент
    перезапустится, не найдёт Secret и не стартует вовсе. И не запись во
    временный файл — это пароль, ему на диске делать нечего.

    stringData, а не data: Kubernetes сам кодирует значения, и base64 руками
    считать не нужно. Поле только на запись, при чтении Secret его не отдаёт.
    """
    patch = json.dumps({"stringData": {
        "login": login,
        "password": password,
        "password-pbkdf2": qbittorrent_pbkdf2(password),
    }})
    return kubectl("patch", "secret", QBT_SECRET, "-n", "media",
                   "--type=merge", "-p", patch, on_line=on_line, timeout=120)


# --- Перенос в другие папки -------------------------------------------------

# Почему это отдельная и осторожная операция, а не просто «повторить установку с
# другим путём». Тома уже созданы и связаны с заявками, а hostPath у связанного
# тома не переехал бы сам. Повторная установка молча оставила бы фильмы в старой
# папке, а библиотека стала бы пустой — человек решил бы, что всё пропало.


def installed_paths() -> tuple[Path, Path]:
    state = config.load_state()
    return (Path(state.get("config_dir") or config.default_config_dir()),
            Path(state.get("media_dir") or config.default_media_dir()))


def _unique_files_size(path: Path) -> int:
    """Размер с учётом жёстких ссылок: один inode считается один раз."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for entry in path.rglob("*"):
        try:
            if not entry.is_file() or entry.is_symlink():
                continue
            info = entry.stat()
            ident = (info.st_dev, info.st_ino)
            if ident in seen:
                continue
            seen.add(ident)
            total += info.st_size
        except OSError:
            continue
    return total


def _same_filesystem(a: Path, b: Path) -> bool:
    """На одной файловой системе перенос мгновенный и места не требует."""
    def device(path: Path) -> int:
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return probe.stat().st_dev
    try:
        return device(a) == device(b)
    except OSError:
        return False


def migration_plan(new_config_dir: Path, new_media_dir: Path) -> dict:
    """Что произойдёт при переносе и можно ли его вообще делать.

    Считается ДО того, как что-то остановлено и сдвинуто: если места не хватает,
    человек должен узнать это раньше, чем сервисы выключатся.
    """
    old_config, old_media = installed_paths()
    moves = [(old, new) for old, new in
             ((old_media, new_media_dir), (old_config, new_config_dir))
             if old.resolve() != new.resolve()]
    if not moves:
        return {"ok": True, "needed": False, "reason": "", "bytes": 0, "instant": True}

    # Непустая папка назначения — отказ. Перенос в неё смешал бы старые файлы с
    # новыми, и разобрать это потом было бы нечем.
    for _, new in moves:
        if new.exists() and any(new.iterdir()):
            return {"ok": False, "needed": True, "bytes": 0, "instant": False,
                    "reason": f"Папка {new} не пуста. Выберите пустую или новую — "
                              f"иначе старые файлы смешаются с тем, что там лежит."}

    need = 0
    instant = True
    for old, new in moves:
        if _same_filesystem(old, new):
            continue        # os.rename в пределах диска: мгновенно и без места
        instant = False
        need += _unique_files_size(old)

    if need:
        probe = new_media_dir
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        if free < need:
            return {"ok": False, "needed": True, "bytes": need, "instant": False,
                    "reason": f"Не хватит места: нужно {need / 1024**3:.1f} ГБ, "
                              f"а свободно {free / 1024**3:.1f} ГБ. "
                              f"Ничего не тронуто, всё осталось как было."}
    return {"ok": True, "needed": True, "bytes": need, "instant": instant, "reason": ""}


def _move_entries(old: Path, new: Path, on_line=None) -> list[tuple[Path, Path]]:
    """Перенос в пределах одной файловой системы: переименование.

    Мгновенно, места не требует и — главное — сохраняет жёсткие ссылки как есть:
    файл не двигается, меняется только запись в каталоге.
    """
    moved: list[tuple[Path, Path]] = []
    new.mkdir(parents=True, exist_ok=True)
    for entry in sorted(old.iterdir()):
        target = new / entry.name
        if on_line:
            on_line(f"  переношу {entry.name}")
        shutil.move(str(entry), str(target))
        moved.append((target, entry))
    return moved


def _copy_preserving_links(old: Path, new: Path, on_line=None) -> None:
    """Копирование на ДРУГОЙ диск, с сохранением жёстких ссылок.

    Почему не shutil.copytree и не shutil.move. Между файловыми системами они
    копируют каждый путь отдельно, и файл, лежавший под двумя именами как одна
    запись на диске, превращается в две. Медиатека занимает вдвое больше места,
    ошибки при этом нет, и весь смысл общего тома для downloads и library
    пропадает — а он и есть причина, по которой они лежат рядом.

    Поэтому запоминаем, какой inode уже скопирован, и для повторов делаем ссылку
    вместо второй копии. Источник не трогаем: удалять его можно только после
    того, как копия целиком удалась.
    """
    copied: dict[tuple[int, int], Path] = {}
    new.mkdir(parents=True, exist_ok=True)
    for source in sorted(old.rglob("*")):
        target = new / source.relative_to(old)
        if source.is_dir() and not source.is_symlink():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        info = source.stat()
        ident = (info.st_dev, info.st_ino)
        if info.st_nlink > 1 and ident in copied:
            os.link(copied[ident], target)
            continue
        if on_line:
            on_line(f"  копирую {source.relative_to(old)}")
        shutil.copy2(source, target)
        if info.st_nlink > 1:
            copied[ident] = target


def _transfer(old: Path, new: Path, on_line=None) -> list[tuple[Path, Path]]:
    """Переносит содержимое папки тем способом, который здесь уместен.

    Возвращает список для отката, если перенос был переименованием. При копировании
    между дисками откатывать нечего: источник остаётся на месте до самого конца.
    """
    if _same_filesystem(old, new):
        return _move_entries(old, new, on_line)
    if on_line:
        on_line("  другой диск — копирую с сохранением жёстких ссылок")
    _copy_preserving_links(old, new, on_line)
    return []


def _undo_moves(moved: list[tuple[Path, Path]], on_line=None) -> None:
    """Откат переименований, в обратном порядке.

    Ошибки здесь глотаем намеренно: мы уже в аварийной ветке, и упасть посреди
    отката хуже, чем вернуть столько, сколько получится.
    """
    for target, original in reversed(moved):
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(original))
            if on_line:
                on_line(f"  вернул {original.name}")
        except OSError:
            continue


def scale_stack(replicas: int, on_line=None) -> None:
    kubectl("scale", "deployment", "--all", "-n", "media",
            f"--replicas={replicas}", on_line=on_line, timeout=180)
    if replicas == 0:
        # Ждём именно исчезновения подов: пока под жив, том смонтирован, и
        # перенос файлов из-под него дал бы половину переехавшей медиатеки.
        kubectl("wait", "--for=delete", "pod", "--all", "-n", "media",
                "--timeout=180s", on_line=on_line, timeout=240)


def stop_media_server(on_line=None) -> None:
    """Останавливает контейнеры, сохраняя настройки, фильмы, PVC и сам k3s."""
    if not k3s_installed():
        raise RuntimeError("k3s ещё не установлен")
    if on_line:
        on_line("Останавливаю медиасервер")
    scale_stack(0, on_line)


def start_media_server(on_line=None) -> None:
    """Возвращает остановленные контейнеры и ждёт их готовности."""
    if not k3s_installed():
        raise RuntimeError("k3s ещё не установлен")
    if on_line:
        on_line("Запускаю медиасервер")
    scale_stack(1, on_line)
    wait_for_pods(on_line=on_line)


def _release_volumes(on_line=None) -> None:
    """Снимает заявки и тома. Данные остаются: у всех томов политика Retain."""
    kubectl("delete", "pvc", "--all", "-n", "media", "--timeout=120s",
            on_line=on_line, timeout=180)
    kubectl("delete", "pv", "-l", "app.kubernetes.io/part-of=media-stack",
            "--timeout=120s", on_line=on_line, timeout=180)


def migrate(new_config_dir: Path, new_media_dir: Path, on_line=None) -> dict:
    """Переносит установку в другие папки. При любом сбое возвращает как было."""
    plan = migration_plan(new_config_dir, new_media_dir)
    if not plan["ok"] or not plan["needed"]:
        return plan

    old_config, old_media = installed_paths()
    if on_line:
        on_line("Останавливаю сервисы — при работающих переносить нельзя")
    scale_stack(0, on_line)
    _release_volumes(on_line)

    moved: list[tuple[Path, Path]] = []
    copied_from: list[Path] = []
    try:
        for old, new in ((old_media, new_media_dir), (old_config, new_config_dir)):
            if old.resolve() == new.resolve():
                continue
            if on_line:
                on_line(f"Переношу {old} -> {new}")
            done = _transfer(old, new, on_line)
            moved += done
            if not done:
                copied_from.append(old)     # копия сделана, источник ещё цел
    except OSError as error:
        if on_line:
            on_line(f"Не получилось перенести: {error}. Возвращаю как было")
        _undo_moves(moved, on_line)
        # Недоделанные копии убираем: источник цел, а половина копии только
        # запутает и займёт место.
        for old in copied_from:
            target = new_media_dir if old == old_media else new_config_dir
            shutil.rmtree(target, ignore_errors=True)
        apply_volumes(old_config, old_media, on_line)
        apply_stack(on_line)
        return {"ok": False, "needed": True, "bytes": plan["bytes"], "instant": False,
                "reason": f"Перенос не удался: {error}. Всё возвращено на место, "
                          f"сервисы запускаются заново."}

    # Источник сносим только теперь, когда копия целиком удалась. До этой строки
    # любой сбой оставлял человека с нетронутой медиатекой на старом месте.
    for old in copied_from:
        shutil.rmtree(old, ignore_errors=True)

    config.save_state(config_dir=str(new_config_dir), media_dir=str(new_media_dir))
    prepare_dirs(new_config_dir, new_media_dir)
    apply_volumes(new_config_dir, new_media_dir, on_line)
    result = apply_stack(on_line)
    if result.returncode == 0:
        apply_identity(on_line)
        wait_for_volumes(on_line=on_line)
    return {"ok": result.returncode == 0, "needed": True, "bytes": plan["bytes"],
            "instant": plan["instant"],
            "reason": "" if result.returncode == 0 else result.stdout[-2000:]}


# --- Установка целиком ------------------------------------------------------

def wait_for_volumes(timeout: int = 180, on_line=None) -> None:
    """Ждём, пока заявки свяжутся с подготовленными томами.

    Без этого ожидания несвязанный том — молчаливый отказ: установка отвечает
    «готово», поды остаются Pending, а человек видит шесть крестиков и кнопку
    «починить», которая ничего не меняет. Связывание — операция контроллера, оно
    не мгновенное, поэтому именно ждём, а не проверяем сразу после apply.
    """
    if on_line:
        on_line("Жду, пока тома свяжутся с заявками")
    r = kubectl("wait", "--for=jsonpath={.status.phase}=Bound", "pvc", "--all",
                "-n", "media", f"--timeout={timeout}s", on_line=on_line,
                timeout=timeout + 60)
    if r.returncode != 0:
        raise RuntimeError(
            "Тома не связались с заявками. Обычно это значит, что выбранная папка "
            "недоступна кластеру. Подробности:\n" + r.stdout[-2000:])


def apply_stack(on_line=None) -> subprocess.CompletedProcess:
    return kubectl("apply", "-k", str(overlay_dir()), on_line=on_line)


# Развёртывания на образах linuxserver.io: они и только они читают PUID/PGID.
# Jellyseerr сюда не входит — образ не от linuxserver.io и этих переменных не знает.
# Список сверяется с манифестами в CI: разъедется — упадёт проверка, а не установка.
LSIO_DEPLOYMENTS = ("jellyfin", "prowlarr", "qbittorrent", "radarr", "sonarr")


def container_identity() -> tuple[str, str]:
    """Под каким пользователем контейнеры должны писать в папки человека.

    В манифестах стенда стоит 1000 — владелец домашней папки на типовой рабочей
    машине. Но папки создаёт установщик от имени того, кто его запустил, и если
    его uid не 1000 (второй аккаунт на машине, раннер CI), контейнеры в них писать
    не смогут. Сообщения об этом человек не увидит: подъедут они нормально, а
    отказ придёт позже и в чужом интерфейсе — Sonarr отвечает 400 на добавление
    корневой папки, не называя причиной права.
    """
    return str(os.getuid()), str(os.getgid())


def apply_identity(on_line=None) -> subprocess.CompletedProcess:
    """Проставляет PUID/PGID по тому, кто запустил установщик.

    Отдельным шагом после apply, а не патчем kustomize: значение известно только
    в момент установки, а патч пришлось бы писать в каталог оверлея — внутри
    собранного бандла он одноразовый, а в репозитории это была бы правка исходников.
    """
    uid, gid = container_identity()
    if on_line:
        on_line(f"Контейнеры будут писать от имени {uid}:{gid}")
    targets = [f"deployment/{name}" for name in LSIO_DEPLOYMENTS]
    return kubectl("set", "env", "-n", "media", *targets,
                   f"PUID={uid}", f"PGID={gid}", on_line=on_line)


def wait_for_pods(timeout: int = 900, on_line=None) -> subprocess.CompletedProcess:
    """Ждёт, пока сервисы поднимутся.

    Между apply и первым ответом Sonarr лежит скачивание семи образов — около
    трёх с половиной гигабайт, на домашнем канале это минуты. Без этого ожидания связывание
    начиналось бы по несуществующим ещё сервисам и падало бы каждый первый раз
    на свежей машине. Возврата не проверяем: не дождались — связывание скажет об
    этом само, и кнопка «Связать сервисы» повторит.
    """
    if on_line:
        on_line("Жду, пока сервисы поднимутся (первый раз это дольше всего)")
    return kubectl("wait", "--for=condition=Ready", "pod", "--all", "-n", "media",
                   f"--timeout={timeout}s", on_line=on_line, timeout=timeout + 60)


# Мониторинг живёт в своём namespace: это единственное, что отличает полный
# профиль от простого, и удаляется он поэтому целиком, одним именем.
MONITORING_NS = "monitoring"


def apply_monitoring(on_line=None) -> subprocess.CompletedProcess:
    # -k, а не -f: мониторингу нужен тот же патч, что и медиастеку.
    return kubectl("apply", "-k", str(monitoring_dir()), on_line=on_line)


def monitoring_installed() -> bool:
    """Стоит ли мониторинг на кластере — вопрос к кластеру, а не к state.json.

    Профиль в состоянии говорит, что человек ВЫБРАЛ, а не что развёрнуто. После
    перехода «полный -> простой» эти два ответа расходятся, и спрашивать нужно
    именно кластер: иначе окно предложит удалить то, чего нет, или промолчит про
    то, что осталось работать.
    """
    return k3s_installed() and \
        kubectl("get", "namespace", MONITORING_NS, timeout=60).returncode == 0


def remove_monitoring(on_line=None) -> subprocess.CompletedProcess:
    """Сносит мониторинг целиком, вместе с namespace.

    Namespace отдельный и ничего чужого в нём нет — ни фильмов, ни настроек
    сервисов, ни тома. Grafana и Prometheus держат данные в emptyDir, то есть
    теряют их и при обычном перезапуске: удалять тут нечего, кроме самих
    графиков за последние часы.
    """
    if on_line:
        on_line("Удаляю мониторинг: выбран простой профиль")
    return kubectl("delete", "namespace", MONITORING_NS, "--ignore-not-found",
                   "--timeout=180s", on_line=on_line, timeout=240)


def install(profile_key: str, config_dir: Path, media_dir: Path, on_line=None,
            drop_monitoring: bool = False, media_login: str = "",
            media_password: str = "") -> dict:
    """Полная установка. Возвращает то, что нужно показать человеку.

    drop_monitoring ставит окно, когда человек переключился с полного профиля на
    простой и подтвердил удаление. Само по себе install этого не решает: удаление
    — потеря, пусть и небольшая, и спрашивать о ней надо до начала работы, а не
    посреди неё из фонового потока.
    """
    prepare_dirs(config_dir, media_dir)
    # Состояние пишем ДО запуска: по нему чинится неудачная установка кнопкой
    # «перепроверить и починить», когда что-то не поднялось с первого раза.
    config.save_state(profile=profile_key, config_dir=str(config_dir),
                      media_dir=str(media_dir))

    # Пункт меню создаём в начале: установка идёт минуты, и всё это время окно
    # должно быть опознаваемым в панели задач, а не серой шестерёнкой.
    if ensure_desktop_entry() and on_line:
        on_line("Добавил приложение в меню")

    ensure_cluster(on_line)
    wait_for_node(on_line=on_line)

    # Namespace нужен раньше Secret'а и томов — они оба в него кладутся.
    kubectl("apply", "-f", str(bundle_root() / "k8s" / "media-stack"
                               / "00-namespace.yaml"), on_line=on_line)
    password = create_qbittorrent_secret(on_line)
    if password:
        prepare_qbittorrent_config(config_dir, config.QBT_DEFAULT_LOGIN, password, on_line)
    apply_volumes(config_dir, media_dir, on_line)
    result = apply_stack(on_line)
    if result.returncode == 0:
        apply_identity(on_line)
        # Заявки создаёт apply_stack, поэтому ждать связывания можно только здесь.
        wait_for_volumes(on_line=on_line)
        if config.PROFILE_BY_KEY[profile_key].with_monitoring:
            # Намеренно НЕ присваиваем в result. Мониторинг — добавка, и его
            # неудача не должна отменять главное. Раньше присваивали, и один
            # занятый порт под Grafana обрывал установку до связывания
            # сервисов: медиасервер оставался несвязанным из-за графиков.
            graphs = apply_monitoring(on_line)
            if graphs.returncode != 0 and on_line:
                on_line("Графики поставить не вышло — остальное это не задевает. "
                        f"Что ответил кластер: {graphs.stdout.strip()[-300:]}")
        elif drop_monitoring:
            remove_monitoring(on_line)

    # Пароль пишем только когда он только что родился: при повторной установке
    # create_qbittorrent_secret вернёт None, и затирать сохранённый было бы потерей.
    # Логин — вместе с ним и по той же причине: они всегда пара, и записанный
    # логин при чужом пароле не открыл бы ничего.
    if password:
        config.save_state(qbittorrent_password=password,
                          qbittorrent_login=config.QBT_DEFAULT_LOGIN)

    # Связываем сервисы сразу: между «установлено» и «можно смотреть» иначе лежит
    # полчаса ручной работы в четырёх чужих интерфейсах. Не получилось — не повод
    # объявлять установку неудачной, кнопка «Связать сервисы» повторит.
    steps = []
    if result.returncode == 0:
        wait_for_pods(on_line=on_line)
        if on_line:
            on_line("Связываю сервисы между собой")
        steps = wire.configure(config_dir, on_line=on_line)
        if media_login and media_password:
            if on_line:
                on_line("Создаю единый аккаунт и настраиваю окно заказов")
            account_steps = wire.configure_account(
                config_dir, media_login, media_password, on_line=on_line)
            steps.extend(account_steps)
            if all(step.ok for step in account_steps):
                config.save_state(media_login=media_login)

    return {
        "ok": result.returncode == 0,
        "steps": steps,
        "qbittorrent_password": password or config.load_state().get("qbittorrent_password"),
        "stdout": result.stdout,
        "stderr": "",
    }


def repair(on_line=None) -> dict:
    """«Перепроверить и починить»: повторный apply приводит кластер к тому, что
    записано в манифестах. Данные не трогает — тома остаются на месте."""
    state = config.load_state()
    profile_key = state.get("profile", "simple")
    config_dir = Path(state.get("config_dir") or config.default_config_dir())
    media_dir = Path(state.get("media_dir") or config.default_media_dir())
    prepare_dirs(config_dir, media_dir)
    apply_volumes(config_dir, media_dir, on_line)
    result = apply_stack(on_line)
    if result.returncode == 0:
        wait_for_volumes(on_line=on_line)
        if config.PROFILE_BY_KEY[profile_key].with_monitoring:
            result = apply_monitoring(on_line)
    return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": "",
            "qbittorrent_password": None, "repair": True}


# --- Предпроверки -----------------------------------------------------------

K3S_DOCS = "https://docs.k3s.io/ru/quick-start"

# Порог с запасом: сами образы занимают около 3 ГБ, остальное — место под фильмы,
# без которого стек запустится и тут же встанет.
MIN_FREE_GB = 15

@dataclass
class Check:
    ok: bool
    title: str      # что проверяли, человеческим языком
    detail: str     # что получилось
    fix: str = ""   # что делать, если не получилось
    link: str = ""  # куда идти читать
    blocking: bool = True


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _disk_check(media_dir: Path | None) -> Check:
    target = media_dir or config.default_media_dir()
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free_gb = shutil.disk_usage(probe).free / 1024 ** 3
    return Check(
        free_gb >= MIN_FREE_GB,
        "Место на диске",
        f"свободно {free_gb:.0f} ГБ на диске с папкой {target}",
        fix=f"Нужно хотя бы {MIN_FREE_GB} ГБ. Освободите место или выберите "
            f"другую папку для фильмов — например, на втором диске.",
    )


# CoreDNS в кластере пересылает всё, что не про кластер, на серверы из resolv.conf
# ноды — то есть на те же, что читает хост. Список виден и до установки кластера,
# поэтому проверка работает на вкладке «Установка» в любой момент.
RESOLV_CONF = (Path("/run/systemd/resolve/resolv.conf"), Path("/etc/resolv.conf"))


def _upstream_resolvers() -> list[str]:
    for path in RESOLV_CONF:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        found = [parts[1] for parts in (line.split() for line in lines)
                 if len(parts) > 1 and parts[0] == "nameserver"]
        if found:
            return found
    return []


def _is_local(server: str) -> bool:
    try:
        return ipaddress.ip_address(server).is_private
    except ValueError:
        return False


def _dns_check() -> Check:
    """Показывает, у кого кластер спрашивает адреса сайтов.

    Проверка не диагностическая, а объясняющая, и это осознанно. Поймать
    избирательную фильтрацию заранее нельзя: домашний роутер отдаёт SERVFAIL
    ровно на те домены, которые фильтрует провайдер, а на все остальные отвечает
    нормально — общий контрольный запрос такое не увидит. Зато человеку, у
    которого Prowlarr пишет «Name does not resolve», строчка с адресом его
    роутера экономит вечер: причина названа до того, как он начнёт искать её в
    настройках Prowlarr, где её нет. Разбор — в MANUAL.md.
    """
    servers = _upstream_resolvers()
    if not servers:
        return Check(True, "Поиск адресов сайтов (DNS)",
                     "список серверов прочитать не удалось", blocking=False)
    shown = ", ".join(servers[:3])
    if any(_is_local(server) for server in servers):
        return Check(True, "Поиск адресов сайтов (DNS)",
                     f"спрашивает {shown}. Первый — сервер вашей сети; если "
                     f"какой-то сайт «не резолвится», дело обычно в нём",
                     blocking=False)
    return Check(True, "Поиск адресов сайтов (DNS)", f"спрашивает {shown}",
                 blocking=False)


def _ports_check() -> Check:
    """Занятый порт — не обязательно помеха.

    На уже установленной машине эти порты занимает сам медиасервер, и это
    единственное правильное состояние. Проверка, которая на них ругалась, не
    отличала своё от чужого: человек с работающим сервером жал «Установить» и
    читал совет остановить прошлую установку — то есть выключить то, что у него
    только что заработало. Поэтому у занятого порта спрашиваем, кто там:
    отвечает по этому адресу наш сервис или посторонняя программа.
    """
    # Grafana нужна только полному профилю, но порт проверяем всегда. Пока она
    # сюда не входила, занятость 30030 не ловилась ничем — Service просто не
    # создавался, и человек оставался без графиков, не понимая почему.
    #
    # Источник той занятости — Traefik — с тех пор выключен флагом в K3S_EXEC,
    # но проверка остаётся: на кластерах, поднятых до этой правки, Traefik никуда
    # не делся, да и занять порт может любая другая программа.
    busy = [s for s in (*config.SERVICES, config.GRAFANA) if _port_busy(s.port)]
    if not busy:
        return Check(True, "Свободные порты", "все нужные порты свободны")

    ours = {s["key"] for s in media.service_status() if s["alive"]}
    # Grafana попадает в service_status() по профилю из состояния, а порт
    # занимает по факту работы. Эти два ответа расходятся ровно тогда, когда
    # человек сменил профиль на простой и на вопрос «удалить графики?» ответил
    # «оставить»: Grafana работает, в состоянии её нет — и предпроверка советовала
    # остановить постороннюю программу на 30030, показывая на его же Grafana.
    # Тот же принцип, что и у monitoring_installed(): спрашиваем кластер.
    if config.GRAFANA.key not in ours and monitoring_installed() \
            and media.service_alive(config.GRAFANA):
        ours.add(config.GRAFANA.key)
    foreign = [s for s in busy if s.key not in ours]
    if not foreign:
        return Check(True, "Порты",
                     "заняты вашим же медиасервером — так и должно быть",
                     blocking=False)
    return Check(
        False, "Свободные порты",
        "заняты: " + ", ".join(f"{s.port} ({s.title})" for s in foreign),
        fix="Эти порты занимает не медиасервер, а другая программа. Две "
            "программы на одном порту не уживаются — ту нужно остановить.",
    )


def _linux_checks() -> list[Check]:
    systemd = Path("/run/systemd/system").is_dir()
    checks = [Check(
        systemd, "systemd", "работает" if systemd else "не найден",
        fix="k3s ставится системной службой, и без systemd его не запустить. "
            "На обычных сборках Ubuntu, Debian и Fedora он есть.",
        link=K3S_DOCS,
    )]
    pkexec = shutil.which("pkexec") is not None
    checks.append(Check(
        pkexec, "Запрос прав администратора",
        "pkexec на месте" if pkexec else "pkexec не найден",
        fix="Установка k3s требует прав администратора, а спросить пароль "
            "программе нечем. Поставьте пакет policykit-1 (или polkit) — либо "
            "выполните команду установки вручную, программа её покажет.",
        blocking=False,
    ))
    if k3s_installed():
        checks.append(Check(True, "k3s", "уже установлен, ставить заново не буду",
                            blocking=False))
    return checks


def preflight(media_dir: Path | None = None) -> list[Check]:
    """Всё, что должно быть в порядке ДО установки. Возвращает список, а не первое
    падение: человеку полезнее увидеть сразу все проблемы, чем чинить их по одной
    и каждый раз запускать проверку заново."""
    checks = _linux_checks()
    checks.append(_disk_check(media_dir))
    checks.append(_ports_check())
    checks.append(_dns_check())
    return checks


def blocking_failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and c.blocking]


def _self_check() -> None:
    """Минимальная проверка того, где логика, а не вызовы внешних команд."""
    # Шаблон томов обязан заполняться целиком: незакрытая скобка или лишний
    # плейсхолдер здесь дешевле поймать, чем на машине пользователя.
    rendered = render_volumes(Path("/tmp/cfg"), Path("/tmp/media"))
    assert "{" not in rendered, "в шаблоне остались незаполненные плейсхолдеры"
    assert '"/tmp/media"' in rendered
    assert '"/tmp/cfg/jellyfin"' in rendered
    assert rendered.count("kind: PersistentVolume") == 7

    # Ожидание узла. Проверяем именно то, из-за чего e2e падал на ровном месте:
    # узел появляется не сразу, а kubectl wait по несуществующему ресурсу
    # выходит мгновенно. Внешних команд тут нет — kubectl и sleep подменены.
    global kubectl, k3s_installed
    real_kubectl, real_k3s_installed, real_sleep = kubectl, k3s_installed, time.sleep
    seen: list[tuple] = []

    # На чистой машине окно спрашивает про прежний мониторинг ещё до установки
    # k3s. Отсутствие команды означает «мониторинга нет», а не аварию callback.
    k3s_installed = lambda: False
    kubectl = lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("kubectl вызван до установки k3s"))
    assert monitoring_installed() is False

    def fake(*args, **_kw):
        seen.append(args)
        gets = [c for c in seen if c[0] == "get"]
        # Узел регистрируется только к третьему опросу.
        out = "" if args[0] == "get" and len(gets) < 3 else "node/home"
        return subprocess.CompletedProcess(args, 0, out)

    kubectl, k3s_installed, time.sleep = fake, real_k3s_installed, lambda _s: None
    try:
        wait_for_node(timeout=60)
        assert len([c for c in seen if c[0] == "get"]) == 3, seen
        assert any(c[0] == "wait" for c in seen), "готовность узла не проверялась"

        # Узел не появился вовсе — обязана быть внятная ошибка, а не молчаливый
        # переход к apply, который упадёт потом и непонятно почему.
        seen.clear()
        kubectl = lambda *a, **k: subprocess.CompletedProcess(a, 0, "")
        try:
            wait_for_node(timeout=0)
        except RuntimeError as e:
            assert "не зарегистрировался" in str(e), e
        else:
            raise AssertionError("пустой список узлов принят за готовый кластер")
        assert not [c for c in seen if c[0] == "wait"], \
            "kubectl wait вызван по несуществующему узлу — ровно то, что чинили"

        # Явная пауза медиасервера сохраняет тома и настройки: выключаются только
        # поды. Повторный запуск возвращает их и дожидается готовности.
        k3s_installed = lambda: True
        kubectl = fake
        seen.clear()
        stop_media_server()
        assert any(c[0] == "scale" and "--replicas=0" in c for c in seen), seen
        assert any(c[0] == "wait" and "--for=delete" in c for c in seen), seen

        seen.clear()
        start_media_server()
        assert any(c[0] == "scale" and "--replicas=1" in c for c in seen), seen
        assert any(c[0] == "wait" and "--for=condition=Ready" in c for c in seen), seen
    finally:
        kubectl, k3s_installed, time.sleep = real_kubectl, real_k3s_installed, real_sleep

    # Пункт меню собирается без обращения к диску: подстановка не должна оставлять
    # незаполненных мест, а имя класса — расходиться с тем, что ставит окно.
    entry = DESKTOP_ENTRY.format(command="/bin/true", icon=DESKTOP_ID, wmclass=DESKTOP_ID)
    assert "{" not in entry, "в шаблоне пункта меню остались плейсхолдеры"
    assert f"StartupWMClass={DESKTOP_ID}" in entry

    # Перенос медиатеки: главное здесь — жёсткие ссылки. Файл, лежащий под двумя
    # именами как одна запись на диске, обязан остаться одной записью и после
    # переезда. Иначе медиатека тихо занимает вдвое больше места.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "src"
        (source / "library").mkdir(parents=True)
        film = source / "film.mkv"
        film.write_bytes(b"x" * 4096)
        os.link(film, source / "library" / "film.mkv")
        assert _unique_files_size(source) == 4096, "hardlink посчитан дважды"

        same = Path(tmp) / "same-fs"
        _transfer(source, same)
        moved = (same / "film.mkv").stat()
        assert moved.st_ino == (same / "library" / "film.mkv").stat().st_ino
        assert not film.exists(), "переименование должно опустошить источник"

        # Копирование между файловыми системами — та ветка, где ссылки легче
        # всего потерять. Проверяем её, если в системе есть tmpfs.
        other = Path("/dev/shm")
        if other.is_dir() and not _same_filesystem(same, other):
            far = other / "home-media-k3s-selfcheck"
            shutil.rmtree(far, ignore_errors=True)
            try:
                _transfer(same, far)
                a = (far / "film.mkv").stat()
                b = (far / "library" / "film.mkv").stat()
                assert a.st_ino == b.st_ino, "ссылка распалась при переезде на другой диск"
                assert (same / "film.mkv").exists(), "источник сносить рано"
            finally:
                shutil.rmtree(far, ignore_errors=True)

    hashed = qbittorrent_pbkdf2("проверка")
    assert hashed.startswith("@ByteArray(") and hashed.endswith(")")
    assert len(generate_password()) == 20

    # Повторная установка может встретить конфиг от прежней попытки. Новый
    # пароль из Secret обязан доехать и туда, иначе окно покажет нерабочий пароль.
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = Path(tmp)
        qbt_config = config_dir / "qbittorrent/qBittorrent/qBittorrent.conf"
        qbt_config.parent.mkdir(parents=True)
        qbt_config.write_text(
            "[Preferences]\nWebUI\\Username=old\n"
            'WebUI\\Password_PBKDF2="@ByteArray(old:hash)"\n'
            "WebUI\\Port=8080\n",
            encoding="utf-8",
        )
        real_urandom = os.urandom
        os.urandom = lambda _size: bytes(range(16))
        commands: list[tuple] = []
        real_kubectl = kubectl
        kubectl = lambda *args, **_kw: (
            commands.append(args) or subprocess.CompletedProcess(args, 0, ""))
        try:
            prepare_qbittorrent_config(config_dir, "new", "new-pass")
        finally:
            os.urandom = real_urandom
            kubectl = real_kubectl
        synced = qbt_config.read_text(encoding="utf-8")
        assert commands[0][0] == "scale" and "--replicas=0" in commands[0], commands
        assert commands[1][0] == "wait" and "--for=delete" in commands[1], commands
        assert "WebUI\\Username=new" in synced
        assert "@ByteArray(old:hash)" not in synced
        assert "WebUI\\Port=8080" in synced, "чужая настройка потерялась"
        assert "N8vsGC4iHgyvNWAnPPYBkH3LrLJnf50/" in synced
    print("install.py self-check: OK")


if __name__ == "__main__":
    _self_check()
