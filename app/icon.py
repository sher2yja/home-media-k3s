"""Иконка приложения. Рисуется кодом и здесь же превращается в PNG.

Почему не файл в репозитории: иконка нужна в двух видах сразу — картинкой в самом
окне (Tk) и файлом на диске для пункта меню. Держать бинарник в git ради
шестидесяти четырёх пикселей, а потом ещё искать его через sys._MEIPASS внутри
бандла — дороже, чем двадцать строк, которые рисуют её на месте.

Почему иконка вообще обязана быть файлом. GNOME Shell 46 больше не берёт картинку
из свойства окна _NET_WM_ICON: окну, которому не нашлось .desktop, он рисует
стандартную заглушку application-x-executable — серую шестерёнку. Единственный
способ показать свою иконку в панели задач и в меню — положить PNG на диск и
сослаться на него из .desktop. См. install.ensure_desktop_entry().
"""

from __future__ import annotations

import struct
import zlib

SIZE = 64
BACKGROUND = (0x1F, 0x6F, 0xEB)
FOREGROUND = (0xFF, 0xFF, 0xFF)


def pixels(size: int = SIZE) -> list[list[tuple[int, int, int]]]:
    """Синий квадрат с белым треугольником «плей»: основание слева, вершина справа."""
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            along = (x - size * 0.31) / (size * 0.41)
            half = (1.0 - along) * size * 0.25
            inside = 0.0 <= along <= 1.0 and abs(y - size / 2) <= half
            row.append(FOREGROUND if inside else BACKGROUND)
        rows.append(row)
    return rows


def png_bytes(size: int = SIZE) -> bytes:
    """Минимальный PNG без сторонних библиотек: truecolor, 8 бит на канал.

    Формат простой ровно настолько, чтобы не тянуть Pillow в зависимости ради
    одной картинки: сигнатура, три блока, у каждого длина, тег, данные и CRC.
    Строка пикселей предваряется нулём — это фильтр None, самый простой из пяти.
    """
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in pixels(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _self_check() -> None:
    data = png_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "не PNG-сигнатура"
    assert data[12:16] == b"IHDR" and data.rstrip().endswith(b"\xaeB`\x82"), "блоки не на месте"
    rows = pixels()
    assert len(rows) == SIZE and len(rows[0]) == SIZE
    assert rows[0][0] == BACKGROUND, "угол должен быть фоном"
    assert rows[SIZE // 2][SIZE // 2] == FOREGROUND, "центр должен быть треугольником"
    print(f"icon.py self-check: OK, PNG {len(data)} байт")


if __name__ == "__main__":
    _self_check()
