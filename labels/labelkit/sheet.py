"""Спуск полос: раскладка наклеек на печатный лист для типографии.

Выход — многостраничный PDF в CMYK: каждая наклейка с вылетом под обрез, контур реза
отдельной плашкой (CutContour, overprint) для плоттерной/режущей резки, служебная
строка на поле листа. Плюс PNG-превью и сводка тиража.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.colors import CMYKColor
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas

from .geometry import load_font, transform
from .render import ROOT, Label, _rl_path, draw_label, layout


@dataclass
class Grid:
    cols: int
    rows: int
    rotated: bool
    cell_w: float
    cell_h: float
    x0: float
    y0: float

    @property
    def per_sheet(self) -> int:
        return self.cols * self.rows


def plan(style: dict, fmt: str, margin: float, gap: float, allow_rotate: bool = True) -> Grid:
    sw, sh = style["sheets"][fmt]["w"], style["sheets"][fmt]["h"]
    W, H = style["size"]["w"], style["size"]["h"]
    best = None
    for rotated in ([False, True] if allow_rotate else [False]):
        w, h = (H, W) if rotated else (W, H)
        cols = int((sw - 2 * margin + gap) // (w + gap))
        rows = int((sh - 2 * margin + gap) // (h + gap))
        if cols < 1 or rows < 1:
            continue
        used_w, used_h = cols * w + (cols - 1) * gap, rows * h + (rows - 1) * gap
        g = Grid(cols, rows, rotated, w, h, (sw - used_w) / 2, (sh - used_h) / 2)
        if best is None or g.per_sheet > best.per_sheet:
            best = g
    if best is None:
        raise ValueError(f"Наклейка {W}x{H} не помещается на {fmt} с полями {margin} мм")
    return best


def _info_text(c, style, text, x, y, cap=1.8):
    font = load_font(tuple(style["fonts"]), str(ROOT))
    path, _ = font.text_mm(text, cap)
    c.setFillColor(CMYKColor(0, 0, 0, 1))
    c.drawPath(_rl_path(c, transform(path, (1, 0, 0, 1, x, y)), 0, 0), stroke=0, fill=1)


def build(items: list[tuple[Label, int]], out_pdf: Path, style: dict, *, fmt: str | None = None,
          margin: float | None = None, gap: float | None = None, cut: bool = True,
          job: str = "", cmyk: bool = True) -> dict:
    """items — [(наклейка, тираж)]. Возвращает сводку."""
    d = style["sheet_defaults"]
    fmt = fmt or d["format"]
    margin = d["margin"] if margin is None else margin
    gap = d["gap"] if gap is None else gap
    bleed = style["bleed"]
    if gap < 2 * bleed:
        raise ValueError(f"Зазор {gap} мм меньше двух вылетов ({2 * bleed} мм) — соседние наклейки перекроются")

    g = plan(style, fmt, margin, gap)
    seq: list[Label] = [lab for lab, q in items for _ in range(int(q))]
    sheets = max(1, math.ceil(len(seq) / g.per_sheet))
    sw, sh = style["sheets"][fmt]["w"], style["sheets"][fmt]["h"]
    W, H = style["size"]["w"], style["size"]["h"]

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    c = rl_canvas.Canvas(str(out_pdf), pagesize=(sw * mm, sh * mm))
    c.setTitle(f"Наклейки · {job}" if job else "Наклейки")
    c.setAuthor("MontajMaster · labelkit")
    scenes: dict[tuple, list] = {}
    warnings: set[str] = set()
    today = dt.date.today().isoformat()
    for s in range(sheets):
        chunk = seq[s * g.per_sheet:(s + 1) * g.per_sheet]
        for i, lab in enumerate(chunk):
            col, row = i % g.cols, i // g.cols
            x = g.x0 + col * (g.cell_w + gap)
            y = sh - g.y0 - (row + 1) * g.cell_h - row * gap  # заполнение сверху вниз
            key = (lab.circuit, lab.top, lab.top2, lab.small, lab.main)
            if key not in scenes:
                scenes[key] = layout(lab, style)
                warnings.update(scenes[key][-1]["warnings"])
            c.saveState()
            if g.rotated:  # наклейка лёжа: поворот на 90° против часовой
                c.translate((x + H) * mm, y * mm)
                c.rotate(90)
            else:
                c.translate(x * mm, y * mm)
            draw_label(c, scenes[key], style, 0, 0, cmyk=cmyk, cut=cut)
            c.restoreState()
        if d.get("info_line", True):
            info = (f"{job or 'наклейки'}  ·  лист {s + 1}/{sheets}  ·  {len(chunk)} шт  ·  {fmt} {sw}x{sh}  ·  "
                    f"{W:g}x{H:g} мм, вылет {bleed:g}  ·  контур: {style['cut']['spot_name']}  ·  {today}")
            _info_text(c, style, info, margin, max(margin / 2 - 0.9, 1.5))
        c.showPage()
    c.save()

    by_circuit: dict[str, int] = {}
    for lab in seq:
        by_circuit[lab.circuit] = by_circuit.get(lab.circuit, 0) + 1
    return {
        "pdf": str(out_pdf), "format": fmt, "sheets": sheets, "per_sheet": g.per_sheet,
        "grid": f"{g.cols}x{g.rows}" + (" (повёрнуты)" if g.rotated else ""),
        "total": len(seq), "unique": len(scenes), "by_circuit": by_circuit,
        "fill": round(len(seq) / (sheets * g.per_sheet) * 100), "warnings": sorted(warnings),
    }
