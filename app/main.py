"""Точка входа приложения-установщика.

Запуск:  python3 app/main.py
"""

from __future__ import annotations

import ui

# sys.path не трогаем: Python сам кладёт каталог запускаемого скрипта первым в
# sys.path, поэтому `import ui` рядом с main.py работает. Ручная вставка пути
# выше импортов заставляла бы любой сортировщик импортов ломать запуск.


def main() -> None:
    ui.App().mainloop()


if __name__ == "__main__":
    main()
