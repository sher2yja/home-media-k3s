"""Окно приложения: четыре вкладки вместо четырёх веб-страниц.

Почему не браузер. Это программа для того, кто сидит за этим компьютером, а не
сервис. Вкладку браузера человек закрывает и теряет, а веб-форма тянула четыре
зависимости ради того, что Tk умеет из коробки. Теперь зависимостей нет вовсе.

Главное правило этого файла: НИЧЕГО ДОЛГОГО В ПОТОКЕ ОКНА. Установка идёт минуты,
опрос шести сервисов — до тридцати секунд; выполненные прямо в обработчике кнопки,
они замораживают окно, и человек решает, что программа повисла. Всё долгое уходит
в отдельный поток, а результат возвращается через очередь, которую разбирает
after() — только так к виджетам обращается один и тот же поток, как требует Tk.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import config
import guides
import icon
import install
import media
import wire

PAD = 10

# Имя класса окна берётся из install, а не пишется здесь второй раз: по нему GNOME
# связывает открытое окно с пунктом меню (StartupWMClass). Разъедутся эти два
# значения — окно потеряет и имя, и иконку, а понять почему будет неоткуда.
WM_CLASS_NAME = install.DESKTOP_ID


def _app_icon() -> tk.PhotoImage:
    """Иконка окна. Пиксели общие с PNG для пункта меню — см. icon.py.

    Заголовок окна её показывает, но панели задач в GNOME 46 этого мало: там
    иконка берётся из .desktop, а не из свойства окна. Поэтому одной этой
    функции недостаточно, нужна ещё install.ensure_desktop_entry().
    """
    rows = icon.pixels()
    image = tk.PhotoImage(width=len(rows[0]), height=len(rows))
    image.put(tuple(tuple(f"#{r:02x}{g:02x}{b:02x}" for r, g, b in row) for row in rows))
    return image


def _scrollable(parent: ttk.Frame) -> ttk.Frame:
    """Область, которая не теряет содержимое на невысоком экране.

    Tk не рисует то, что не поместилось: виджеты, спакованные последними,
    просто исчезают. На вкладке «Установка» так пропадала кнопка «Установить» —
    программа выглядела сломанной, и заметить это на своей машине нельзя,
    там окно открывается целиком.
    """
    canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
    bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas, padding=PAD)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    def _fit(_event=None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))
        # Ширину тянем за холстом: иначе текст с wraplength считает перенос по
        # своей ширине, а не по окну, и строки уезжают вправо.
        canvas.itemconfigure(window, width=canvas.winfo_width())

    inner.bind("<Configure>", _fit)
    canvas.bind("<Configure>", _fit)
    return inner


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__(className=WM_CLASS_NAME)
        self.title("Домашний медиасервер")
        # Ссылку на иконку держим сами: PhotoImage без ссылки собирает сборщик
        # мусора, и окно молча остаётся без иконки.
        self._icon = _app_icon()
        self.iconphoto(True, self._icon)
        self.geometry("820x680")
        self.minsize(660, 520)

        # Вместо cookie с токеном Jellyfin: приложение однопользовательское и
        # живёт до закрытия, хранить вход между запусками незачем.
        self.jellyfin_user: str | None = None
        self.jellyfin_name: str | None = None

        self._events: queue.Queue = queue.Queue()
        self._busy = False
        self._recheck_job: str | None = None

        # Колесо привязываем один раз на всё окно: Tk не передаёт событие вверх
        # по вложенности, и привязка к самой области не сработала бы над её
        # содержимым — то есть почти нигде.
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(sequence, self._on_wheel)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)
        self._build_setup_tab()
        self._build_dashboard_tab()
        self._build_movies_tab()
        self._build_help_tab()

        self.notebook.select(1 if config.installed() else 0)
        self.after(100, self._drain)
        if config.installed():
            self.refresh_dashboard()
        else:
            self.refresh_checks()

    # --- Фоновая работа -----------------------------------------------------

    def _log(self, line: str) -> None:
        """Вызывается из чужого потока — поэтому кладёт в очередь, а не в виджет."""
        self._events.put(("line", line))

    def run_bg(self, work, done) -> bool:
        """work(log) выполняется в отдельном потоке, done(result) — в потоке окна.

        Исключение не теряется и не всплывает в никуда: оно приезжает в done тем
        же путём, что и нормальный результат, и там превращается в понятный текст.
        """
        if self._busy:
            return False
        self._busy = True
        self._set_busy(True)

        def worker() -> None:
            try:
                result = work(self._log)
            except Exception as exc:  # noqa: BLE001 — до окна должна доехать любая
                result = exc
            self._events.put(("done", (done, result)))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _on_wheel(self, event) -> None:
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            # У поля с текстом своя прокрутка — чужую поверх неё не крутим.
            if isinstance(widget, (tk.Text, tk.Listbox)):
                return
            if isinstance(widget, tk.Canvas):
                break
            widget = widget.master
        if widget is None:
            return
        # Linux шлёт колесо кнопками 4 и 5, Windows и macOS — величиной delta.
        up = event.num == 4 if event.num in (4, 5) else event.delta > 0
        widget.yview_scroll(-1 if up else 1, "units")

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                self._append_log(payload)
            else:
                self._busy = False
                self._set_busy(False)
                callback, result = payload
                callback(result)
        self.after(100, self._drain)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in (self.install_button, self.recheck_button,
                       self.repair_button, self.wire_button):
            button.configure(state=state)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _append_log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # --- Вкладка «Установка» ------------------------------------------------

    def _build_setup_tab(self) -> None:
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text="Установка")
        tab = _scrollable(outer)

        ttk.Label(tab, text="Установка домашнего медиасервера",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(tab, wraplength=760, foreground="#555", text=(
            "Сначала проверим, всё ли готово на этом компьютере. Чего не хватает — "
            "программа поставит сама; где понадобится подтверждение, она спросит."
        )).pack(anchor="w", pady=(0, PAD))

        self.checks_frame = ttk.LabelFrame(tab, text="Проверки", padding=PAD)
        self.checks_frame.pack(fill="x")

        self.recheck_button = ttk.Button(tab, text="Проверить снова",
                                         command=self.refresh_checks)
        self.recheck_button.pack(anchor="w", pady=(6, PAD))

        profiles = ttk.LabelFrame(tab, text="Что установить", padding=PAD)
        profiles.pack(fill="x")
        self.profile = tk.StringVar(value=config.PROFILES[0].key)
        for item in config.PROFILES:
            ttk.Radiobutton(profiles, text=item.title, value=item.key,
                            variable=self.profile).pack(anchor="w")
            ttk.Label(profiles, text=item.blurb, wraplength=720,
                      foreground="#555").pack(anchor="w", padx=(20, 0), pady=(0, 6))

        folders = ttk.LabelFrame(tab, text="Куда складывать", padding=PAD)
        folders.pack(fill="x", pady=(PAD, 0))
        self.media_dir = self._folder_row(
            folders, "Фильмы (эта папка вырастет до сотен гигабайт)",
            config.default_media_dir())
        self.config_dir = self._folder_row(
            folders, "Настройки (небольшая папка, трогать не нужно)",
            config.default_config_dir())

        # Проверка места считается для ВЫБРАННОЙ папки, поэтому её надо
        # пересчитывать при смене пути, а не только по кнопке: иначе человек
        # переносит медиатеку на другой диск, а видит свободное место на старом.
        # Пауза — чтобы не пересчитывать на каждую букву при ручном вводе.
        self.media_dir.trace_add("write", self._media_dir_changed)

        # Логин и пароль торрента. Пустые поля — прежнее поведение: программа
        # придумает пароль сама и покажет его в конце. Заполнять их незачем
        # почти никому, поэтому они и стоят ниже папок и подписаны как
        # необязательные, а не встречают человека первым делом.
        creds = ttk.LabelFrame(tab, text="Логин и пароль торрента (необязательно)",
                               padding=PAD)
        creds.pack(fill="x", pady=(PAD, 0))
        ttk.Label(creds, foreground="#555", wraplength=720, text=(
            "Оставьте пустыми — программа придумает пароль сама и покажет его в "
            "конце установки. Заполните, если хотите свои. Только латинские "
            "буквы, цифры и знаки: русские буквы qBittorrent примет, а Sonarr и "
            "Radarr после этого перестанут к нему подключаться."
        )).pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(creds)
        row.pack(fill="x")
        ttk.Label(row, text="Логин").pack(side="left")
        self.qbt_login = tk.StringVar()
        ttk.Entry(row, textvariable=self.qbt_login, width=18).pack(side="left",
                                                                  padx=(6, 16))
        ttk.Label(row, text="Пароль").pack(side="left")
        self.qbt_password = tk.StringVar()
        ttk.Entry(row, textvariable=self.qbt_password, width=24).pack(side="left",
                                                                     padx=(6, 0))

        self.install_button = ttk.Button(tab, text="Установить", command=self.do_install)
        self.install_button.pack(anchor="w", pady=PAD)
        self.progress = ttk.Progressbar(tab, mode="indeterminate")
        self.progress.pack(fill="x")

        ttk.Label(tab, text="Что происходит:", foreground="#555").pack(
            anchor="w", pady=(PAD, 2))
        self.log = scrolledtext.ScrolledText(tab, height=10, state="disabled",
                                             wrap="word")
        self.log.pack(fill="both", expand=True)

    def _folder_row(self, parent: ttk.Frame, label: str, default: Path) -> tk.StringVar:
        ttk.Label(parent, text=label, foreground="#555").pack(anchor="w", pady=(6, 2))
        row = ttk.Frame(parent)
        row.pack(fill="x")
        var = tk.StringVar(value=str(default))
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Выбрать…",
                   command=lambda: self._pick_folder(var)).pack(side="left", padx=(6, 0))
        return var

    def _pick_folder(self, var: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if chosen:
            var.set(chosen)

    def _media_dir_changed(self, *_args) -> None:
        if self._recheck_job:
            self.after_cancel(self._recheck_job)
        self._recheck_job = self.after(700, self.refresh_checks)

    def refresh_checks(self) -> None:
        self._recheck_job = None
        for child in self.checks_frame.winfo_children():
            child.destroy()
        ttk.Label(self.checks_frame, text="Проверяю…").pack(anchor="w")
        # Значение переменной читаем ЗДЕСЬ, в потоке окна, и передаём в поток уже
        # обычной строкой. Tk-переменная, прочитанная из чужого потока, роняет
        # проверку с «main thread is not in main loop» — а выглядит это как
        # «предпроверки не работают», без всякого намёка на причину.
        media_dir = Path(self.media_dir.get())
        if not self.run_bg(lambda log: install.preflight(media_dir), self._show_checks):
            # Занято другой работой — не теряем запрос, а повторяем позже.
            self._recheck_job = self.after(500, self.refresh_checks)

    def _show_checks(self, checks) -> None:
        for child in self.checks_frame.winfo_children():
            child.destroy()
        if isinstance(checks, Exception):
            ttk.Label(self.checks_frame, text=f"Не удалось проверить: {checks}",
                      foreground="#b3261e", wraplength=740).pack(anchor="w")
            return
        for check in checks:
            row = ttk.Frame(self.checks_frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text="✓" if check.ok else "✕", width=2,
                      foreground="#1a7f37" if check.ok else "#b3261e").pack(side="left")
            body = ttk.Frame(row)
            body.pack(side="left", fill="x", expand=True)
            ttk.Label(body, text=f"{check.title} — {check.detail}",
                      wraplength=720).pack(anchor="w")
            if not check.ok and check.fix:
                ttk.Label(body, text=check.fix, foreground="#555",
                          wraplength=720).pack(anchor="w")
            if not check.ok and check.link:
                self._link(body, check.link)

    def _link(self, parent: ttk.Frame, url: str) -> None:
        label = ttk.Label(parent, text=url, foreground="#0b57d0", cursor="hand2")
        label.pack(anchor="w")
        label.bind("<Button-1>", lambda _event: webbrowser.open(url))

    def do_install(self) -> None:
        media_dir = Path(self.media_dir.get())
        config_dir = Path(self.config_dir.get())
        profile = self.profile.get()
        if config.installed() and self._offer_migration(config_dir, media_dir):
            return
        # Не пытаемся ставить поверх непройденных проверок: сообщение kubectl о
        # том, почему не вышло, человеку ничего не скажет, а наши проверки — скажут.
        failures = install.blocking_failures(install.preflight(media_dir))
        if failures:
            self._show_checks(install.preflight(media_dir))
            messagebox.showwarning(
                "Пока не всё готово",
                "\n\n".join(f"{c.title}: {c.detail}\n{c.fix}" for c in failures))
            return
        login, password = self.qbt_login.get().strip(), self.qbt_password.get()
        if login or password:
            problem = wire.check_credentials(login, password)
            if problem:
                messagebox.showwarning("Логин и пароль торрента", problem)
                return
        drop_monitoring = self._ask_drop_monitoring(profile)
        if drop_monitoring is None:
            return
        self.run_bg(
            lambda log: install.install(profile, config_dir, media_dir, on_line=log,
                                        drop_monitoring=drop_monitoring,
                                        qbt_login=login, qbt_password=password),
            self._install_done)

    def _ask_drop_monitoring(self, profile_key: str) -> bool | None:
        """Спрашивает про мониторинг при переходе на простой профиль.

        None значит «человек передумал ставить вообще». Спрашиваем до начала
        работы и в потоке окна: из фонового потока messagebox показывать нельзя,
        а молча снести то, что человек включал осознанно, — тем более.

        Вопрос задаётся по состоянию КЛАСТЕРА, а не по записи в state.json:
        именно расхождение между ними и было дефектом — профиль переключался, а
        мониторинг оставался работать и есть память.
        """
        if config.PROFILE_BY_KEY[profile_key].with_monitoring:
            return False
        if not install.monitoring_installed():
            return False
        answer = messagebox.askyesnocancel(
            "Убрать графики?",
            "Сейчас установлен полный профиль, и вы выбрали простой.\n\n"
            "Страница с графиками (Grafana) будет удалена, вместе с ней "
            "освободится около 500 МБ памяти. Фильмы, настройки сервисов и "
            "пароль торрента не тронутся.\n\n"
            "«Да» — удалить графики и поставить простой профиль.\n"
            "«Нет» — оставить графики работать, профиль всё равно станет "
            "простым.\n"
            "«Отмена» — ничего не делать.")
        return None if answer is None else bool(answer)

    def _offer_migration(self, config_dir: Path, media_dir: Path) -> bool:
        """Папки меняют уже после установки. Возвращает True, если взяли работу на себя.

        Молча переустанавливать нельзя: тома уже созданы, фильмы остались бы в
        старой папке, а библиотека стала бы пустой — без единого сообщения об
        ошибке. Поэтому сначала объясняем, потом предлагаем перенести, и только
        с согласия трогаем данные.
        """
        plan = install.migration_plan(config_dir, media_dir)
        if not plan["needed"]:
            return False
        old_config, old_media = install.installed_paths()

        if not plan["ok"]:
            messagebox.showerror("Перенести не получится", plan["reason"])
            return True

        size = (" Переносить почти нечего, это быстро." if plan["instant"]
                else f" Нужно перенести {plan['bytes'] / 1024 ** 3:.1f} ГБ, "
                     f"это может занять долго.")
        if not messagebox.askyesno("Папки уже выбраны при установке", (
                f"Сейчас фильмы лежат в {old_media},\n"
                f"настройки — в {old_config}.\n\n"
                "Сами по себе они не переедут: если просто поменять путь, "
                "библиотека станет пустой, а файлы останутся на старом месте.\n\n"
                f"Перенести всё в новые папки?{size}\n\n"
                "На время переноса сервисы будут остановлены. Если что-то пойдёт "
                "не так, программа вернёт всё как было.")):
            # Отказались — возвращаем поля к тому, что реально установлено, чтобы
            # на экране не осталось вранья про несуществующие папки.
            self.media_dir.set(str(old_media))
            self.config_dir.set(str(old_config))
            return True

        self.run_bg(lambda log: install.migrate(config_dir, media_dir, on_line=log),
                    self._migration_done)
        return True

    def _migration_done(self, result) -> None:
        if isinstance(result, Exception):
            messagebox.showerror("Не получилось", str(result))
            return
        if result["ok"]:
            messagebox.showinfo("Перенесено",
                                "Файлы на новом месте, сервисы запускаются заново.")
            self.notebook.select(1)
            self.refresh_dashboard()
        else:
            messagebox.showerror("Не получилось", result["reason"])

    def _install_done(self, result) -> None:
        if isinstance(result, install.ManualStepRequired):
            self._manual_step(result)
            return
        if isinstance(result, Exception):
            messagebox.showerror("Не получилось", str(result))
            return
        if not result.get("ok"):
            messagebox.showerror(
                "Не получилось",
                "Кластер сообщил вот что. Если ничего не понятно — это нормально, "
                "покажите текст из окна ниже тому, кто ставил вам систему.")
            return
        password = result.get("qbittorrent_password")
        if password:
            self._password_dialog(password)
        self.notebook.select(1)
        self.refresh_dashboard()

    def _manual_step(self, error: install.ManualStepRequired) -> None:
        """Запасной путь на случай, когда прав получить не вышло: показать готовую
        команду, а не оставить человека наедине с «отказано в доступе»."""
        window = tk.Toplevel(self)
        window.title("Нужно выполнить вручную")
        frame = ttk.Frame(window, padding=PAD)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=str(error), wraplength=520).pack(anchor="w")
        entry = ttk.Entry(frame, width=70)
        entry.insert(0, error.command)
        entry.configure(state="readonly")
        entry.pack(fill="x", pady=PAD)
        ttk.Button(frame, text="Скопировать команду",
                   command=lambda: self._copy(error.command)).pack(anchor="w")

    def _password_dialog(self, password: str) -> None:
        window = tk.Toplevel(self)
        window.title("Запишите пароль торрента")
        frame = ttk.Frame(window, padding=PAD)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, wraplength=460, text=(
            "Пароль понадобится, только если вы полезете в настройки торрента. "
            "Логин admin, пароль:")).pack(anchor="w")
        entry = ttk.Entry(frame, width=30, font=("TkFixedFont", 12))
        entry.insert(0, password)
        entry.configure(state="readonly")
        entry.pack(pady=PAD)
        ttk.Button(frame, text="Скопировать",
                   command=lambda: self._copy(password)).pack(anchor="w")
        ttk.Label(frame, foreground="#555", wraplength=460, text=(
            "Пароль сохранён на этом компьютере, посмотреть снова можно на вкладке "
            "«Как пользоваться».")).pack(anchor="w", pady=(PAD, 0))

    def _copy(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    # --- Вкладка «Настройки сервера» -----------------------------------------

    def _build_dashboard_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=PAD)
        self.notebook.add(tab, text="Настройки сервера")

        ttk.Label(tab, text="Ваш медиасервер",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        # Надпись говорит правду, а не «настройте все шесть»: три сервиса
        # настраивает wire.configure, и человек, пошедший искать в них настройки,
        # потратит время впустую. Список берётся из guides.NEEDS_SETUP, чтобы не
        # разъехаться с подвкладками.
        need = ", ".join(config.BY_KEY[k].title for k in guides.NEEDS_SETUP)
        ttk.Label(tab, foreground="#555", wraplength=760, text=(
            f"Три сервера нужно настроить самому — {need}: в них нужен ваш "
            "выбор, и за вас его никто не сделает. Откройте подвкладку и следуйте "
            "инструкции. Остальные программа настроила сама; их подвкладки нужны, "
            "только если захотите проверить.\n"
            "Всё это работает на этом компьютере — пока он включён."
        )).pack(anchor="w", pady=(0, PAD))

        self.server_tabs = ttk.Notebook(tab)
        self.server_tabs.pack(fill="both", expand=True)
        self._build_services_subtab()
        self.guide_texts = {}
        for key in guides.ORDER:
            self._build_guide_subtab(key)
        self._fill_guides()

    def _build_services_subtab(self) -> None:
        tab = ttk.Frame(self.server_tabs, padding=PAD)
        self.server_tabs.add(tab, text="Все сервисы")

        self.services_frame = ttk.Frame(tab)
        self.services_frame.pack(fill="both", expand=True)

        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=PAD)
        ttk.Button(buttons, text="Обновить",
                   command=self.refresh_dashboard).pack(side="left")
        self.wire_button = ttk.Button(buttons, text="Связать сервисы",
                                      command=self.do_wire)
        self.wire_button.pack(side="left", padx=(6, 0))
        self.repair_button = ttk.Button(buttons, text="Перепроверить и починить",
                                        command=self.do_repair)
        self.repair_button.pack(side="left", padx=(6, 0))
        ttk.Label(tab, foreground="#555", wraplength=740, text=(
            "«Связать сервисы» — рассказывает сервисам друг о друге: поиск, торрент, "
            "куда складывать готовое. Делается при установке само, кнопка нужна, "
            "если что-то не успело подняться или если вы добавили сайт в Prowlarr.\n"
            "«Перепроверить и починить» возвращает сервисы к правильным настройкам. "
            "Фильмы и настройки при этом не пропадают."
        )).pack(anchor="w")

    def _build_guide_subtab(self, key: str) -> None:
        service = config.BY_KEY[key]
        tab = ttk.Frame(self.server_tabs, padding=PAD)
        self.server_tabs.add(tab, text=service.title)
        text = scrolledtext.ScrolledText(tab, wrap="word", state="disabled",
                                         height=16)
        text.pack(fill="both", expand=True)
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=(PAD, 0))
        ttk.Button(buttons, text=f"Открыть {service.title}",
                   command=lambda s=service: webbrowser.open(config.service_url(s))
                   ).pack(side="left")
        if key == "qbittorrent":
            ttk.Button(buttons, text="Сменить логин и пароль",
                       command=self.ask_credentials).pack(side="left", padx=(6, 0))
        self.guide_texts[key] = text

    def ask_credentials(self) -> None:
        """Смена логина и пароля торрента на работающей установке.

        Отдельное окошко, а не поля прямо в подвкладке: это действие, которое
        трогает три места сразу (торрент, Secret, оба *arr), и случайно нажать
        его не должно быть легко.
        """
        if not config.installed():
            messagebox.showinfo("Медиасервер ещё не установлен",
                                "Логин и пароль появятся после установки.")
            return
        window = tk.Toplevel(self)
        window.title("Смена логина и пароля торрента")
        window.transient(self)
        frame = ttk.Frame(window, padding=PAD)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, wraplength=440, foreground="#555", text=(
            "Поменяется в трёх местах сразу: в самом qBittorrent, в сохранённых "
            "настройках и в Sonarr с Radarr — иначе они перестанут к нему "
            "подключаться и заказы молча перестанут скачиваться.\n\n"
            "Только латинские буквы, цифры и знаки."
        )).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, PAD))

        login = tk.StringVar(value=config.qbittorrent_login())
        password = tk.StringVar()
        for row, (label, var) in enumerate((("Логин", login),
                                            ("Новый пароль", password)), start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=var, width=32).grid(row=row, column=1,
                                                              sticky="ew", pady=2)

        def apply() -> None:
            problem = wire.check_credentials(login.get().strip(), password.get())
            if problem:
                messagebox.showwarning("Не подходит", problem, parent=window)
                return
            new_login, new_password = login.get().strip(), password.get()
            window.destroy()
            self.notebook.select(0)   # лог живёт на первой вкладке
            self.run_bg(
                lambda log: install.change_qbittorrent_credentials(
                    new_login, new_password, on_line=log),
                self._credentials_done)

        ttk.Button(frame, text="Сменить", command=apply).grid(
            row=3, column=0, sticky="w", pady=(PAD, 0))
        ttk.Button(frame, text="Отмена", command=window.destroy).grid(
            row=3, column=1, sticky="e", pady=(PAD, 0))

    def _credentials_done(self, steps) -> None:
        if isinstance(steps, Exception):
            messagebox.showerror("Не вышло сменить", str(steps))
            return
        failed = [s for s in steps if not s.ok]
        self._fill_guides()
        if failed:
            messagebox.showwarning(
                "Сменилось не везде",
                "\n\n".join(f"{s.title}: {s.detail}" for s in failed)
                + "\n\nНажмите «Связать сервисы» на подвкладке «Все сервисы».")
        else:
            messagebox.showinfo("Готово",
                                "Логин и пароль сменены. Sonarr и Radarr уже знают "
                                "новые — заказы продолжат скачиваться.")

    def _fill_guides(self) -> None:
        """Перерисовывает инструкции. Вызывается и при «Обновить»: до установки
        ключей API и пароля ещё нет, и в тексте на их месте стоит оговорка."""
        state = config.load_state()
        config_dir = Path(state.get("config_dir") or config.default_config_dir())
        keys = {name: media.arr_api_key(config_dir, name)
                for name in ("sonarr", "radarr")}
        password = state.get("qbittorrent_password")
        for key, widget in self.guide_texts.items():
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", "\n".join(guides.lines(key, keys, password)))
            widget.configure(state="disabled")

    def refresh_dashboard(self) -> None:
        self._fill_guides()
        for child in self.services_frame.winfo_children():
            child.destroy()
        ttk.Label(self.services_frame, text="Спрашиваю сервисы…").pack(anchor="w")
        self.run_bg(lambda log: media.service_status(), self._show_services)

    def _show_services(self, services) -> None:
        for child in self.services_frame.winfo_children():
            child.destroy()
        if isinstance(services, Exception):
            ttk.Label(self.services_frame, text=f"Не удалось опросить: {services}",
                      foreground="#b3261e").pack(anchor="w")
            return
        for group, title in ((True, "Чем пользоваться"),
                             (False, "Служебное — работает само")):
            ttk.Label(self.services_frame, text=title,
                      font=("TkDefaultFont", 11, "bold")).pack(anchor="w", pady=(PAD, 2))
            for service in (s for s in services if s["user_facing"] is group):
                self._service_row(service)

    def _service_row(self, service: dict) -> None:
        row = ttk.Frame(self.services_frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="✓" if service["alive"] else "✕", width=2,
                  foreground="#1a7f37" if service["alive"] else "#b3261e"
                  ).pack(side="left")
        ttk.Button(row, text=service["title"], width=14,
                   command=lambda: webbrowser.open(service["url"])).pack(side="left")
        ttk.Label(row, text=f"{service['text']}. {service['blurb']}", wraplength=600,
                  foreground="#555").pack(side="left", padx=(8, 0))

    def do_wire(self) -> None:
        self.notebook.select(0)   # лог живёт на первой вкладке — переключаемся к нему
        self.run_bg(lambda log: wire.configure(on_line=log), self._wire_done)

    def _wire_done(self, steps) -> None:
        if isinstance(steps, Exception):
            messagebox.showerror("Не получилось", str(steps))
            return
        failed = [s for s in steps if not s.ok]
        text = "\n".join(f"{'✓' if s.ok else '✕'} {s.title}: {s.detail}" for s in steps)
        if failed:
            messagebox.showwarning(
                "Связано не всё",
                text + "\n\nЧаще всего это значит, что сервис ещё поднимается. "
                       "Подождите пару минут и нажмите кнопку снова.")
        else:
            messagebox.showinfo("Сервисы связаны", text)
        self.notebook.select(1)

    def do_repair(self) -> None:
        self.notebook.select(0)   # лог живёт на первой вкладке — переключаемся к нему
        self.run_bg(lambda log: install.repair(on_line=log), self._repair_done)

    def _repair_done(self, result) -> None:
        if isinstance(result, Exception):
            messagebox.showerror("Не получилось", str(result))
            return
        messagebox.showinfo(
            "Готово" if result.get("ok") else "Не получилось",
            "Сервисы приведены к правильным настройкам." if result.get("ok")
            else "Подробности — в окне «Что происходит» на вкладке «Установка».")
        self.notebook.select(1)
        self.refresh_dashboard()

    # --- Вкладка «Где мой фильм» --------------------------------------------

    def _build_movies_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=PAD)
        self.notebook.add(tab, text="Где мой фильм")

        ttk.Label(tab, text="Где мой фильм",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")

        self.login_frame = ttk.LabelFrame(tab, text="Вход", padding=PAD)
        self.login_frame.pack(fill="x", pady=(PAD, 0))
        ttk.Label(self.login_frame, wraplength=740, text=(
            "Войдите тем же логином и паролем, что и в Jellyfin — тогда увидите "
            "свои заказы. Отдельной учётной записи здесь нет: аккаунты живут в "
            "Jellyfin, программа просто спрашивает у него."
        )).pack(anchor="w", pady=(0, 6))
        self.jf_login = tk.StringVar()
        self.jf_password = tk.StringVar()
        form = ttk.Frame(self.login_frame)
        form.pack(anchor="w")
        ttk.Label(form, text="Логин").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.jf_login, width=24).grid(row=0, column=1)
        ttk.Label(form, text="Пароль").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(form, textvariable=self.jf_password, show="•", width=24).grid(
            row=1, column=1, pady=(4, 0))
        ttk.Button(form, text="Войти", command=self.do_login).grid(
            row=0, column=2, rowspan=2, padx=(8, 0))

        ttk.Button(tab, text="Обновить", command=self.refresh_movies).pack(
            anchor="w", pady=PAD)
        self.movies_frame = ttk.Frame(tab)
        self.movies_frame.pack(fill="both", expand=True)

    def do_login(self) -> None:
        login, password = self.jf_login.get(), self.jf_password.get()
        self.run_bg(lambda log: media.jellyfin_login(login, password),
                    self._login_done)

    def _login_done(self, who) -> None:
        if isinstance(who, Exception) or not who or not who.get("user_id"):
            messagebox.showwarning("Не вышло войти",
                                   "Jellyfin не принял этот логин или пароль.")
            return
        self.jellyfin_user = who["user_id"]
        self.jellyfin_name = who.get("name") or self.jf_login.get()
        self.jf_password.set("")
        self.login_frame.pack_forget()
        self.refresh_movies()

    def refresh_movies(self) -> None:
        for child in self.movies_frame.winfo_children():
            child.destroy()
        ttk.Label(self.movies_frame, text="Смотрю, что качается…").pack(anchor="w")
        state = config.load_state()
        config_dir = Path(state.get("config_dir") or config.default_config_dir())
        self.run_bg(
            lambda log: media.where_is_my_movie(
                config_dir, jellyfin_user_id=self.jellyfin_user),
            self._show_movies)

    def _show_movies(self, items) -> None:
        for child in self.movies_frame.winfo_children():
            child.destroy()
        if isinstance(items, Exception):
            ttk.Label(self.movies_frame, text=f"Не удалось спросить: {items}",
                      foreground="#b3261e").pack(anchor="w")
            return
        if not items:
            ttk.Label(self.movies_frame, wraplength=740, text=(
                "Сейчас ничего не качается.\n\nЕсли вы только что заказали фильм — "
                "подождите пару минут: система сначала ищет, где его взять. "
                "Заказы делаются в Jellyseerr."
            )).pack(anchor="w")
            return
        for item in items:
            row = ttk.Frame(self.movies_frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=item["title"], font=("TkDefaultFont", 10, "bold"),
                      wraplength=400).pack(side="left")
            ttk.Label(row, text=item["status"], wraplength=320,
                      foreground="#1a7f37" if item["done"] else "#555").pack(
                          side="left", padx=(8, 0))

    # --- Вкладка «Как пользоваться» -----------------------------------------

    def _build_help_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=PAD)
        self.notebook.add(tab, text="Как пользоваться")
        self.help_text = scrolledtext.ScrolledText(tab, wrap="word", state="disabled")
        self.help_text.pack(fill="both", expand=True)
        command = " ".join(install.kubectl_command("get", "pods", "-n", "media"))
        ttk.Button(tab, text="Скопировать команду состояния",
                   command=lambda: self._copy(command)).pack(anchor="w", pady=(PAD, 0))
        self._fill_help(command)

    def _fill_help(self, command: str) -> None:
        state = config.load_state()
        password = state.get("qbittorrent_password")
        profile = config.PROFILE_BY_KEY.get(state.get("profile", ""))
        blocks = [
            "НАСТРОЙКА СЕРВЕРОВ ПЕРЕЕХАЛА",
            ("Пошаговые инструкции — что нажать в Jellyfin, Prowlarr и "
             "Jellyseerr — теперь на вкладке «Настройки сервера», по подвкладке "
             "на каждый сервис. Там же видно, какие сервисы настраивать не "
             "нужно вовсе."),
            "",
            "Здесь осталось то, чем пользуются каждый день.",
            "",
            "=" * 60,
            "",
            "ПОСМОТРЕТЬ ФИЛЬМ",
            "1. Откройте Jellyfin на вкладке «Настройки сервера».",
            "2. Выберите фильм и нажмите «Смотреть».",
            "",
            ("С телевизора или телефона: установите приложение Jellyfin и укажите "
             f"ему адрес этого компьютера в домашней сети, порт "
             f"{config.BY_KEY['jellyfin'].port}."),
            "",
            "ЗАКАЗАТЬ ФИЛЬМ, КОТОРОГО НЕТ",
            "1. Откройте Jellyseerr на вкладке «Настройки сервера».",
            "2. Найдите фильм или сериал через поиск и нажмите «Запросить».",
            "",
            ("Дальше система ищет и качает сама. Следить за этим — на вкладке "
             "«Где мой фильм»."),
            "",
        ]
        if password:
            blocks += ["ПАРОЛЬ КАЧАЛКИ",
                       f"Логин admin, пароль {password}.",
                       ("Программа уже вписала его куда надо. Он нужен, только "
                        "если вы полезете в настройки qBittorrent сами."), ""]
        if profile and profile.with_monitoring:
            blocks += ["ГРАФИКИ НАГРУЗКИ",
                       f"Grafana: {config.service_url(config.GRAFANA)}",
                       config.GRAFANA.blurb, ""]
        blocks += [
            "=" * 60,
            "",
            "ЧАСТЫЕ ВОПРОСЫ",
            "",
            "Что будет, если нажать «Установить» ещё раз?",
            ("Ничего не сломается. Пароль торрента останется прежним, фильмы и "
             "настройки — на месте: программа просто заново приведёт сервисы к "
             "правильному состоянию. Но чаще нужна не эта кнопка, а "
             "«Перепроверить и починить» на вкладке «Настройки сервера» — то же самое, "
             "только сразу."),
            "",
            "Что будет, если выключить компьютер?",
            ("Настраивать заново не придётся ничего. Сервер поднимется сам при "
             "следующем включении: он стоит системной службой, а фильмы и "
             "настройки лежат обычными папками на диске. Первую пару минут после "
             "включения на вкладке «Настройки сервера» будут крестики — это сервисы "
             "просыпаются. Сама программа для работы сервера не нужна вовсе, "
             "она пульт."),
            "",
            "ЕСЛИ ПОНАДОБИЛАСЬ КОМАНДНАЯ СТРОКА",
            ("Обычно она не нужна — всё делается кнопками. Но если вас попросили "
             "выполнить команду, вот та, что показывает состояние сервисов:"),
            "",
            f"    {command}",
            "",
            "Она ничего не меняет — только выводит список работающих сервисов.",
        ]
        self.help_text.configure(state="normal")
        self.help_text.delete("1.0", "end")
        self.help_text.insert("1.0", "\n".join(blocks))
        self.help_text.configure(state="disabled")
