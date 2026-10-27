"""Векторная геометрия наклейки: контуры в мм (y вверх) и текст в кривых.

Все фигуры — списки команд ('M', x, y) / ('L', x, y) / ('C', x1, y1, x2, y2, x, y) / ('Z',),
которые одинаково выводятся и в PDF (reportlab), и в SVG.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont

K = 0.5522847498  # коэффициент Безье для четверти окружности


def rounded_rect(x: float, y: float, w: float, h: float, r: float) -> list[tuple]:
    r = max(0.0, min(r, w / 2, h / 2))
    k = r * K
    return [
        ("M", x + r, y),
        ("L", x + w - r, y),
        ("C", x + w - r + k, y, x + w, y + r - k, x + w, y + r),
        ("L", x + w, y + h - r),
        ("C", x + w, y + h - r + k, x + w - r + k, y + h, x + w - r, y + h),
        ("L", x + r, y + h),
        ("C", x + r - k, y + h, x, y + h - r + k, x, y + h - r),
        ("L", x, y + r),
        ("C", x, y + r - k, x + r - k, y, x + r, y),
        ("Z",),
    ]


def transform(path: list[tuple], m: tuple[float, ...]) -> list[tuple]:
    """Аффинное преобразование m = (a, b, c, d, e, f): x' = a*x + c*y + e, y' = b*x + d*y + f."""
    a, b, c, d, e, f = m
    out = []
    for cmd in path:
        if cmd[0] == "Z":
            out.append(cmd)
            continue
        pts = cmd[1:]
        new = []
        for i in range(0, len(pts), 2):
            x, y = pts[i], pts[i + 1]
            new += [a * x + c * y + e, b * x + d * y + f]
        out.append((cmd[0], *new))
    return out


def bbox(path: list[tuple]) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for cmd in path:
        pts = cmd[1:]
        xs += pts[0::2]
        ys += pts[1::2]
    if not xs:
        return 0, 0, 0, 0
    return min(xs), min(ys), max(xs), max(ys)


class _Pen(BasePen):
    def __init__(self, glyphset, dx: float):
        super().__init__(glyphset)
        self.dx = dx
        self.path: list[tuple] = []

    def _moveTo(self, p):
        self.path.append(("M", p[0] + self.dx, p[1]))

    def _lineTo(self, p):
        self.path.append(("L", p[0] + self.dx, p[1]))

    def _curveToOne(self, p1, p2, p3):
        self.path.append(("C", p1[0] + self.dx, p1[1], p2[0] + self.dx, p2[1], p3[0] + self.dx, p3[1]))

    def _closePath(self):
        self.path.append(("Z",))


class Font:
    def __init__(self, path: Path):
        self.path = path
        self.tt = TTFont(str(path))
        self.upm = self.tt["head"].unitsPerEm
        self.cmap = self.tt.getBestCmap()
        self.glyphs = self.tt.getGlyphSet()
        self.hmtx = self.tt["hmtx"]
        cap = getattr(self.tt["OS/2"], "sCapHeight", 0) or 0
        if not cap:
            gname = self.cmap.get(ord("Н")) or self.cmap.get(ord("H"))
            pen = _Pen(self.glyphs, 0)
            self.glyphs[gname].draw(pen)
            cap = bbox(pen.path)[3]
        self.cap = cap
        name = self.tt["name"].getDebugName(4) or path.stem
        self.name = name

    def missing(self, text: str) -> list[str]:
        return sorted({ch for ch in text if ch != " " and ord(ch) not in self.cmap})

    def outline(self, text: str, tracking_em: float = 0.0) -> tuple[list[tuple], float]:
        """Текст в кривых, единицы шрифта, базовая линия y=0. Возвращает (путь, ширина)."""
        x = 0.0
        path: list[tuple] = []
        for ch in text:
            gname = self.cmap.get(ord(ch)) or self.cmap.get(ord("?"))
            if gname is None:
                continue
            pen = _Pen(self.glyphs, x)
            self.glyphs[gname].draw(pen)
            path += pen.path
            x += self.hmtx[gname][0] + tracking_em * self.upm
        if text:
            x -= tracking_em * self.upm
        return path, x

    def text_mm(self, text: str, cap_mm: float, tracking_em: float = 0.0):
        """Текст, отмасштабированный так, что высота прописных = cap_mm. -> (путь, ширина_мм)."""
        raw, w = self.outline(text, tracking_em)
        s = cap_mm / self.cap
        return transform(raw, (s, 0, 0, s, 0, 0)), w * s


@lru_cache(maxsize=8)
def load_font(candidates: tuple[str, ...], base: str) -> Font:
    for c in candidates:
        p = Path(c) if Path(c).is_absolute() else Path(base) / c
        if p.exists():
            return Font(p)
    raise FileNotFoundError(
        "Не найден ни один шрифт из списка: " + ", ".join(candidates)
        + ". Положите Buyan-Regular.ttf в labels/fonts/."
    )


def rotate_m(deg: float, cx: float = 0, cy: float = 0) -> tuple[float, ...]:
    r = math.radians(deg)
    a, b = math.cos(r), math.sin(r)
    return (a, b, -b, a, cx - a * cx + b * cy, cy - b * cx - a * cy)
