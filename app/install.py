"""Установка стека на машину пользователя: кластер k3s, тома, пароль qBittorrent.

Одна и та же последовательность на обеих ОС — меняется только то, ЧЕМ запускается
команда. На Linux k3s ставится в систему прямо, на Windows под него подкладывается
WSL2, и `kubectl` вызывается внутри дистрибутива. Вся разница между платформами
собрана в двух функциях, `kubectl_command()` и `host_path()`; выше по коду
ветвления по ОС нет.

Здесь же лежит ЕДИНСТВЕННАЯ в проекте реализация PBKDF2-хеша для qBittorrent.
Её же вызывает infra/vms/scripts/08-qbittorrent-secret.sh, который делает то же
самое для стенда. Две копии одной криптографии — способ однажды тихо разъехаться
и получить «неверный пароль» без объяснений.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import config

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
    return bundle_root() / "k8s" / "monitoring"


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

WSL_DISTRO = "Ubuntu"

# Флаг Windows: без него каждая вызванная команда мигает чёрным окном консоли.
# На Linux атрибута нет, поэтому берём через getattr.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RebootRequired(Exception):
    """Windows включил компоненты WSL и требует перезагрузки. Не ошибка: установка
    продолжится после перезапуска приложения, состояние уже сохранено."""


class ManualStepRequired(Exception):
    """Шаг требует прав, которых приложение не получило. Несёт готовую команду —
    человеку остаётся скопировать её, а не сочинять самому."""

    def __init__(self, message: str, command: str) -> None:
        super().__init__(message)
        self.command = command


def _decode(raw: bytes) -> str:
    """wsl.exe пишет вывод в UTF-16LE, а не в UTF-8, и обычное декодирование даёт
    строку с нулевым байтом между каждой буквой. Проверка на «Ubuntu in wsl -l»
    из-за этого молча не срабатывает."""
    if raw[:1] == b"\x00" or (len(raw) > 1 and raw[1:2] == b"\x00"):
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def run(cmd: list[str], *, timeout: int = 1800,
        on_line=None) -> subprocess.CompletedProcess:
    """Запускает команду, отдавая вывод построчно в on_line.

    Построчно, а не одним куском в конце: установка идёт минуты, и окно, в котором
    ничего не происходит, человек закрывает. Ошибки не глотаем и returncode
    разбираем сами — при разборе проблемы нужен сырой текст, а не наш пересказ.
    """
    if on_line:
        on_line("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=_NO_WINDOW)
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    for raw in proc.stdout:
        line = _decode(raw).rstrip("\r\n")
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
    if config.is_windows():
        return ["wsl", "-d", WSL_DISTRO, "-u", "root", "--", "k3s", "kubectl", *args]
    return ["k3s", "kubectl", *args]


def kubectl(*args: str, on_line=None, timeout: int = 900) -> subprocess.CompletedProcess:
    return run(kubectl_command(*args), on_line=on_line, timeout=timeout)


def windows_to_wsl(path) -> str:
    r"""D:\Movies\Кино -> /mnt/d/Movies/Кино.

    Отдельной функцией, а не внутри host_path, чтобы её можно было проверить на
    Linux: PureWindowsPath разбирает windows-пути на любой ОС.
    """
    w = PureWindowsPath(path)
    drive = w.drive.rstrip(":").lower()
    rest = "/".join(w.parts[1:])
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


def host_path(path: Path) -> str:
    """Путь, каким его увидит кластер. На Windows кластер живёт внутри WSL2, и
    диски Windows видны ему как /mnt/<буква>. Это касается и папок пользователя,
    и самих манифестов: бандл распаковывается во временный каталог Windows."""
    return windows_to_wsl(path) if config.is_windows() else str(path)


# --- Подъём кластера --------------------------------------------------------

K3S_INSTALL_URL = "https://get.k3s.io"

# Kubeconfig по умолчанию доступен только root. Приложение работает от обычного
# пользователя, и без этого флага каждый вызов kubectl требовал бы повышения прав.
# SHORTCUT: kubeconfig становится читаемым всем локальным пользователям, а это
# полный доступ к кластеру. Для домашней машины принято сознательно.
K3S_EXEC = "--write-kubeconfig-mode 0644"


def k3s_installed() -> bool:
    if config.is_windows():
        r = run(["wsl", "-d", WSL_DISTRO, "-u", "root", "--",
                 "sh", "-c", "command -v k3s"], timeout=60)
        return r.returncode == 0
    return shutil.which("k3s") is not None


def _download_installer() -> Path:
    """Скачиваем скрипт в файл, а не запускаем `curl | sh`. Разница не
    косметическая: скачанный файл можно показать человеку и он же остаётся на
    диске, если установка упала и нужно разбираться."""
    path = Path(tempfile.gettempdir()) / "k3s-install.sh"
    with urllib.request.urlopen(K3S_INSTALL_URL, timeout=60) as r:
        path.write_bytes(r.read())
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


# --- Windows: цепочка WSL2 --------------------------------------------------

def _wsl_available() -> bool:
    if not shutil.which("wsl"):
        return False
    return run(["wsl", "--status"], timeout=60).returncode == 0


def _distro_installed() -> bool:
    r = run(["wsl", "-l", "-q"], timeout=60)
    return WSL_DISTRO.lower() in r.stdout.lower()


def _systemd_enabled() -> bool:
    """Без systemd k3s не поставить службой, а без службы он не переживёт
    перезапуск компьютера — то есть медиасервер не поднимется сам."""
    r = run(["wsl", "-d", WSL_DISTRO, "-u", "root", "--",
             "sh", "-c", "test -d /run/systemd/system"], timeout=120)
    return r.returncode == 0


def _run_elevated(powershell_command: str) -> None:
    """Перезапускает одну команду с повышением прав через штатное окно UAC.

    ShellExecuteW возвращает управление сразу и кода завершения не отдаёт —
    поэтому вызывающий обязан после неё перепроверить состояние, а не считать,
    что всё получилось.
    """
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe",
        f"-NoProfile -ExecutionPolicy Bypass -Command {powershell_command}", None, 1)
    if int(rc) <= 32:   # документированный признак отказа, в т.ч. «человек нажал Нет»
        raise ManualStepRequired(
            "Нужны права администратора. Запустите PowerShell от имени "
            "администратора и выполните команду.", powershell_command)


def ensure_wsl(on_line=None) -> None:
    """Проводит Windows по цепочке до состояния «внутри есть Ubuntu с systemd».

    Каждый шаг проверяется отдельно и делается только если нужен: человек может
    прийти сюда с уже включённым WSL, с включённым но без дистрибутива, или после
    перезагрузки посреди установки.
    """
    if not _wsl_available():
        if on_line:
            on_line("Включаю компоненты WSL2 — понадобится подтверждение администратора")
        _run_elevated("wsl --install --no-launch")
        # Компоненты Windows включаются только после перезагрузки. Флаг переживёт
        # закрытие приложения, поэтому после перезапуска мы окажемся не здесь,
        # а сразу на следующем шаге.
        config.save_state(wsl_reboot_pending=True)
        raise RebootRequired(
            "Windows включил компоненты WSL2. Перезагрузите компьютер и запустите "
            "программу снова — установка продолжится с этого места.")

    if config.load_state().get("wsl_reboot_pending"):
        config.save_state(wsl_reboot_pending=False)

    if not _distro_installed():
        if on_line:
            on_line(f"Ставлю {WSL_DISTRO} внутрь WSL2, это несколько минут")
        # --no-launch: без него установщик открывает консоль и ждёт, пока человек
        # придумает логин и пароль linux-пользователя. Пользователь внутри не нужен —
        # всё делается от root, а сервисы и так работают под PUID=1000.
        r = run(["wsl", "--install", "-d", WSL_DISTRO, "--no-launch"], on_line=on_line)
        if r.returncode != 0 or not _distro_installed():
            raise ManualStepRequired(
                f"Не удалось поставить {WSL_DISTRO} внутрь WSL2.",
                f"wsl --install -d {WSL_DISTRO} --no-launch")

    if not _systemd_enabled():
        if on_line:
            on_line("Включаю systemd внутри WSL2")
        run(["wsl", "-d", WSL_DISTRO, "-u", "root", "--", "sh", "-c",
             "printf '[boot]\\nsystemd=true\\n' > /etc/wsl.conf"], on_line=on_line)
        # Перечитать /etc/wsl.conf дистрибутив может только при полном останове WSL:
        # обычный выход из оболочки его не перезапускает.
        run(["wsl", "--shutdown"], on_line=on_line, timeout=120)
        if not _systemd_enabled():
            raise ManualStepRequired(
                "systemd внутри WSL2 не включился. Обычно это старая версия WSL.",
                "wsl --update")


def install_k3s_windows(on_line=None) -> None:
    ensure_wsl(on_line)
    if k3s_installed():
        return
    if on_line:
        on_line("Ставлю k3s внутрь WSL2")
    r = run(["wsl", "-d", WSL_DISTRO, "-u", "root", "--", "sh", "-c",
             f'curl -sfL {K3S_INSTALL_URL} | INSTALL_K3S_EXEC="{K3S_EXEC}" sh -'],
            on_line=on_line)
    if r.returncode != 0:
        raise RuntimeError(f"Установщик k3s внутри WSL2 завершился с кодом {r.returncode}")


def ensure_cluster(on_line=None) -> None:
    """Единственное место с ветвлением по ОС в самом сценарии установки."""
    if config.is_windows():
        install_k3s_windows(on_line)
    elif not k3s_installed():
        install_k3s_linux(on_line)


def wait_for_node(timeout: int = 300, on_line=None) -> None:
    """Сразу после установки kubelet ещё регистрируется, и apply падает на
    «no such host». Ждём готовности ноды, а не спим фиксированное время."""
    if on_line:
        on_line("Жду, пока кластер станет готов")
    r = kubectl("wait", "--for=condition=Ready", "node", "--all",
                f"--timeout={timeout}s", on_line=on_line, timeout=timeout + 60)
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
    values = {"media": json.dumps(host_path(media_dir))}
    for service in config.SERVICES:
        values[service.key] = json.dumps(host_path(config_dir / service.key))
    return tmpl.format(**values)


def apply_volumes(config_dir: Path, media_dir: Path, on_line=None) -> None:
    rendered = render_volumes(config_dir, media_dir)
    path = Path(tempfile.gettempdir()) / "home-media-k3s-volumes.yaml"
    path.write_text(rendered, encoding="utf-8")
    r = kubectl("apply", "-f", host_path(path), on_line=on_line)
    if r.returncode != 0:
        raise RuntimeError("Не удалось создать тома: " + r.stdout[-2000:])


# --- Пароль qBittorrent в кластере ------------------------------------------

QBT_SECRET = "qbittorrent-webui"


def create_qbittorrent_secret(on_line=None) -> str | None:
    """Возвращает новый пароль либо None, если Secret уже был.

    Идемпотентность здесь не про аккуратность: пароль сохранён в download client
    Sonarr и Radarr, и молчаливая смена сломала бы им скачивание. Дальше конфиг
    qBittorrent засевает initContainer seed-webui-config, читая хеш отсюда.
    """
    if kubectl("get", "secret", "-n", "media", QBT_SECRET, timeout=120).returncode == 0:
        return None
    password = generate_password()
    r = kubectl("create", "secret", "generic", QBT_SECRET, "-n", "media",
                f"--from-literal=password={password}",
                f"--from-literal=password-pbkdf2={qbittorrent_pbkdf2(password)}",
                on_line=on_line, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("Не удалось создать пароль качалки: " + r.stdout[-2000:])
    return password


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
    return kubectl("apply", "-k", host_path(overlay_dir()), on_line=on_line)


def apply_monitoring(on_line=None) -> subprocess.CompletedProcess:
    # Каталогом, а не по файлам: числовой префикс задаёт порядок, а apply -f по
    # каталогу применяет их в алфавитном порядке — namespace 00 идёт первым.
    return kubectl("apply", "-f", host_path(monitoring_dir()), on_line=on_line)


def install(profile_key: str, config_dir: Path, media_dir: Path, on_line=None) -> dict:
    """Полная установка. Возвращает то, что нужно показать человеку."""
    prepare_dirs(config_dir, media_dir)
    # Состояние пишем ДО запуска: по нему продолжается установка после перезагрузки
    # Windows и по нему же чинится неудачная установка кнопкой «перепроверить».
    config.save_state(profile=profile_key, config_dir=str(config_dir),
                      media_dir=str(media_dir))

    ensure_cluster(on_line)
    wait_for_node(on_line=on_line)

    # Namespace нужен раньше Secret'а и томов — они оба в него кладутся.
    kubectl("apply", "-f", host_path(bundle_root() / "k8s" / "media-stack"
                                     / "00-namespace.yaml"), on_line=on_line)
    password = create_qbittorrent_secret(on_line)
    apply_volumes(config_dir, media_dir, on_line)
    result = apply_stack(on_line)
    if result.returncode == 0:
        # Заявки создаёт apply_stack, поэтому ждать связывания можно только здесь.
        wait_for_volumes(on_line=on_line)
        if config.PROFILE_BY_KEY[profile_key].with_monitoring:
            result = apply_monitoring(on_line)

    # Пароль пишем только когда он только что родился: при повторной установке
    # create_qbittorrent_secret вернёт None, и затирать сохранённый было бы потерей.
    if password:
        config.save_state(qbittorrent_password=password)
    return {
        "ok": result.returncode == 0,
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

WSL_DOCS = "https://learn.microsoft.com/ru-ru/windows/wsl/install"
K3S_DOCS = "https://docs.k3s.io/ru/quick-start"

# Порог с запасом: сами образы занимают около 3 ГБ, остальное — место под фильмы,
# без которого стек запустится и тут же встанет.
MIN_FREE_GB = 15

# wsl --install появился в Windows 10 build 19041 (версия 2004). На более старых
# сборках вся цепочка ниже неприменима, и честнее сказать это сразу.
MIN_WINDOWS_BUILD = 19041


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


def _ports_check() -> Check:
    busy = [s for s in config.SERVICES if _port_busy(s.port)]
    return Check(
        not busy,
        "Свободные порты",
        "все нужные порты свободны" if not busy
        else "заняты: " + ", ".join(f"{s.port} ({s.title})" for s in busy),
        fix="Эти порты уже занимает другая программа. Чаще всего это прошлая "
            "установка — её нужно остановить, а не ставить вторую поверх.",
    )


def _windows_checks() -> list[Check]:
    """Цепочка WSL2 показывается по шагам специально: человек видит, где именно
    он находится, а не одно «не готово» на всю установку. Ни один шаг не
    блокирующий — приложение умеет пройти их само, кнопкой «Установить»."""
    build = int(platform.version().rsplit(".", 1)[-1] or 0)
    checks = [Check(
        build >= MIN_WINDOWS_BUILD,
        "Версия Windows",
        f"сборка {build}",
        fix="Нужна Windows 10 версии 2004 или новее (или Windows 11). "
            "Обновите систему через Центр обновления.",
        link=WSL_DOCS,
    )]
    if not checks[0].ok:
        return checks

    if config.load_state().get("wsl_reboot_pending"):
        checks.append(Check(
            False, "Перезагрузка",
            "компоненты WSL2 включены, но ещё не работают",
            fix="Перезагрузите компьютер и запустите программу снова — "
                "установка продолжится с этого места.",
        ))
        return checks

    wsl = _wsl_available()
    checks.append(Check(wsl, "WSL2", "работает" if wsl else "не установлен",
                        fix="Программа включит его сама при установке. "
                            "Понадобится подтверждение администратора и перезагрузка.",
                        link=WSL_DOCS, blocking=False))
    if not wsl:
        return checks

    distro = _distro_installed()
    checks.append(Check(distro, f"Linux внутри WSL2 ({WSL_DISTRO})",
                        "установлен" if distro else "не установлен",
                        fix="Программа поставит его сама при установке.",
                        blocking=False))
    if distro:
        systemd = _systemd_enabled()
        checks.append(Check(systemd, "systemd внутри WSL2",
                            "включён" if systemd else "выключен",
                            fix="Программа включит его сама при установке.",
                            blocking=False))
    return checks


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
    checks = _windows_checks() if config.is_windows() else _linux_checks()
    checks.append(_disk_check(media_dir))
    checks.append(_ports_check())
    return checks


def blocking_failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and c.blocking]


def _self_check() -> None:
    """Минимальная проверка того, где логика, а не вызовы внешних команд."""
    assert windows_to_wsl(r"D:\Movies") == "/mnt/d/Movies"
    assert windows_to_wsl(r"C:\Users\Ann\Видео\кино") == "/mnt/c/Users/Ann/Видео/кино"
    assert windows_to_wsl("E:\\") == "/mnt/e"
    # Путь бандла PyInstaller выглядит именно так, и он тоже обязан переводиться.
    assert windows_to_wsl(r"C:\Users\Ann\AppData\Local\Temp\_MEI123\deploy\k3s") \
        == "/mnt/c/Users/Ann/AppData/Local/Temp/_MEI123/deploy/k3s"

    # Шаблон томов обязан заполняться целиком: незакрытая скобка или лишний
    # плейсхолдер здесь дешевле поймать, чем на машине пользователя.
    rendered = render_volumes(Path("/tmp/cfg"), Path("/tmp/media"))
    assert "{" not in rendered, "в шаблоне остались незаполненные плейсхолдеры"
    assert '"/tmp/media"' in rendered
    assert '"/tmp/cfg/jellyfin"' in rendered
    assert rendered.count("kind: PersistentVolume") == 7

    hashed = qbittorrent_pbkdf2("проверка")
    assert hashed.startswith("@ByteArray(") and hashed.endswith(")")
    assert len(generate_password()) == 20
    print("install.py self-check: OK")


if __name__ == "__main__":
    _self_check()
