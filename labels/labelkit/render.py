"""Сборка наклейки по шаблону и вывод в PDF (печать, CMYK) / SVG (для Affinity) / PNG (превью)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.colors import CMYKColor, CMYKColorSep, Color
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfgen.canvas import FILL_EVEN_ODD

from .geometry import bbox, load_font, rotate_m, rounded_rect, transform

ROOT = Path(__file__).resolve().parent.parent  # labels/


def load_style(path: Path | None = None) -> dict:
    return json.loads((path or ROOT / "config" / "style.json").read_text(encoding="utf-8"))


@dataclass
class Label:
    circuit: str
    top: str = ""
    main: str = ""
    top2: str = ""
    small: str = ""
    id: str = ""

    @property
    def title(self) -> str:
        """Имя как в файлах Affinity: «ХВС КУХНЯ ПЫЛЕСОС»."""
        parts = [self.circuit, self.top, self.top2, self.small, self.main.replace("|", " ")]
        return " ".join(p for p in parts if p).replace(".", " ").replace("  ", " ").strip()

    def main_lines(self) -> list[str]:
        return [s.strip() for s in self.main.replace("\n", "|").split("|") if s.strip()]


# ------------------------------------------------------------------ компоновка

def layout(label: Label, style: dict) -> list[dict]:
    """Сцена наклейки: список элементов в мм (начало — левый нижний угол линии реза)."""
    font = load_font(tuple(style["fonts"]), str(ROOT))
    W, H = style["size"]["w"], style["size"]["h"]
    r, bleed = style["corner_radius"], style["bleed"]
    if label.circuit not in style["circuits"]:
        raise ValueError(f"Неизвестный контур «{label.circuit}». Есть: {', '.join(style['circuits'])}")

    scene: list[dict] = [
        {"kind": "bg", "layer": "FON", "path": rounded_rect(-bleed, -bleed, W + 2 * bleed, H + 2 * bleed, r + bleed)},
    ]
    k = style["kant"]
    a, w = k["inset"], k["width"]
    ring = rounded_rect(a, a, W - 2 * a, H - 2 * a, max(r - a, 0)) + \
        rounded_rect(a + w, a + w, W - 2 * (a + w), H - 2 * (a + w), max(r - a - w, 0))
    scene.append({"kind": "kant", "layer": style["circuits"][label.circuit].get("layer", label.circuit + " КАНТ"),
                  "path": ring, "circuit": label.circuit})

    warnings: list[str] = []
    for t in (label.top, label.top2, label.small, label.main):
        miss = font.missing(t.replace("|", ""))
        if miss:
            warnings.append(f"в шрифте {font.name} нет символов: {' '.join(miss)}")

    # верхний блок: ВЕРХНЯЯ СТРОКА / НИЖНЯЯ СТРОКА / МАЛАЯ СТРОКА НИЗ
    y = H - style["top"]["y_top"]
    block_bottom = y
    first = True
    for key, layer in (("top", "ВЕРХНЯЯ СТРОКА"), ("top2", "НИЖНЯЯ СТРОКА"), ("small", "МАЛАЯ СТРОКА НИЗ")):
        text = getattr(label, key)
        if not text:
            continue
        st = style[key]
        cap = st["cap"]
        if not first:
            y -= st.get("gap", 1.0)
        maxw = W - 2 * st["side_margin"]
        path, tw = font.text_mm(text, cap, st["tracking"])
        if tw > maxw:
            cap *= maxw / tw
            path, tw = font.text_mm(text, cap, st["tracking"])
        base = y - cap
        scene.append({"kind": "text", "layer": layer, "text": text, "cap": cap,
                      "path": transform(path, (1, 0, 0, 1, (W - tw) / 2, base)),
                      "anchor": ((W) / 2, base), "rotate": 0})
        y = base
        block_bottom = base
        first = False

    # основная надпись — вертикально, читается сверху вниз
    lines = label.main_lines()
    if lines:
        st = style["main"]
        n = len(lines)
        cap = st["cap"] if n == 1 else st["cap"] * 0.6
        gap = st["line_gap"]
        y_hi = (block_bottom - st["gap_after_top"]) if not first else H - st["bottom_margin"]
        y_lo = st["bottom_margin"]
        avail = y_hi - y_lo
        max_thick = W - 2 * (k["inset"] + k["width"] + 1.5)
        lens = [font.text_mm(t, cap, st["tracking"])[1] for t in lines]
        scale = min(1.0, avail / max(lens), max_thick / (n * cap + (n - 1) * gap))
        cap *= scale
        gap *= scale
        lens = [L * scale for L in lens]
        thick = n * cap + (n - 1) * gap
        maxL = max(lens)
        cy = H - st["center_from_top"]
        cy = min(cy, y_hi - maxL / 2)
        cy = max(cy, y_lo + maxL / 2)
        for i, (text, L) in enumerate(zip(lines, lens)):
            path, _ = font.text_mm(text, cap, st["tracking"])
            path = transform(path, rotate_m(-90))  # (x, y) -> (y, -x)
            left = (W + thick) / 2 - cap - i * (cap + gap)
            scene.append({"kind": "text", "layer": "ПОТРЕБИТЕЛИ ВОДА", "text": text, "cap": cap,
                          "path": transform(path, (1, 0, 0, 1, left, cy + L / 2)),
                          "anchor": (left, cy + L / 2), "rotate": -90})

    scene.append({"kind": "cut", "layer": "CUT LINE", "path": rounded_rect(0, 0, W, H, r)})
    scene.append({"kind": "meta", "font": font.name, "warnings": warnings})
    return scene


# ------------------------------------------------------------------ PDF

def _rl_path(c, path, ox, oy):
    p = c.beginPath()
    for cmd in path:
        op = cmd[0]
        pts = [v * mm for v in cmd[1:]]
        for i in range(0, len(pts), 2):
            pts[i] += ox
            pts[i + 1] += oy
        if op == "M":
            p.moveTo(*pts)
        elif op == "L":
            p.lineTo(*pts)
        elif op == "C":
            p.curveTo(*pts)
        else:
            p.close()
    return p


def _color(spec: dict, cmyk: bool):
    if cmyk:
        c_, m_, y_, k_ = (v / 100 for v in spec["cmyk"])
        return CMYKColor(c_, m_, y_, k_)
    r, g, b = (v / 255 for v in spec["rgb"])
    return Color(r, g, b)


def draw_label(c, scene: list[dict], style: dict, x: float, y: float, *, cmyk: bool = True,
               bleed: bool = True, cut: bool = True) -> None:
    """Рисует наклейку на canvas reportlab; (x, y) — левый нижний угол линии реза, в pt."""
    W, H = style["size"]["w"], style["size"]["h"]
    bg = _color(style["background"], cmyk)
    fg = _color(style["text_color"], cmyk)
    for el in scene:
        kind = el["kind"]
        if kind == "bg":
            path = el["path"] if bleed else rounded_rect(0, 0, W, H, style["corner_radius"])
            c.setFillColor(bg)
            c.drawPath(_rl_path(c, path, x, y), stroke=0, fill=1)
        elif kind == "kant":
            col = _color(style["circuits"][el["circuit"]], cmyk)
            c.saveState()
            c.clipPath(_rl_path(c, el["path"], x, y), stroke=0, fill=0, fillMode=FILL_EVEN_ODD)
            k = style["kant"]
            c.linearGradient(x, y + k["inset"] * mm, x, y + H * k["fade_end"] * mm, (col, bg), extend=True)
            c.restoreState()
        elif kind == "text":
            c.setFillColor(fg)
            c.drawPath(_rl_path(c, el["path"], x, y), stroke=0, fill=1, fillMode=FILL_EVEN_ODD)
        elif kind == "cut" and cut:
            cs = style["cut"]
            cc, mm_, yy, kk = (v / 100 for v in cs["cmyk"])
            c.saveState()
            if cmyk:
                c.setStrokeColor(CMYKColorSep(cc, mm_, yy, kk, spotName=cs["spot_name"], density=1))
                c.setStrokeOverprint(True)
            else:
                c.setStrokeColorRGB(1, 0, 1)
            c.setLineWidth(cs["line_width_pt"])
            c.drawPath(_rl_path(c, el["path"], x, y), stroke=1, fill=0)
            c.restoreState()


def label_pdf(label: Label, out: Path, style: dict, *, cmyk: bool = True, cut: bool = True) -> list[str]:
    """Одиночная наклейка под печать: формат = размер + вылеты, контур реза отдельной плашкой."""
    scene = layout(label, style)
    W, H, b = style["size"]["w"], style["size"]["h"], style["bleed"]
    out.parent.mkdir(parents=True, exist_ok=True)
    c = rl_canvas.Canvas(str(out), pagesize=((W + 2 * b) * mm, (H + 2 * b) * mm))
    c.setTitle(label.title)
    c.setAuthor("MontajMaster · labelkit")
    c.setSubject(f"{W:g}x{H:g} мм, вылет {b:g} мм, контур реза: {style['cut']['spot_name']}")
    draw_label(c, scene, style, b * mm, b * mm, cmyk=cmyk, cut=cut)
    c.showPage()
    c.save()
    return scene[-1]["warnings"]


# ------------------------------------------------------------------ SVG (для Affinity)

def _svg_d(path, H, ox=0.0):
    parts = []
    for cmd in path:
        op = cmd[0]
        if op == "Z":
            parts.append("Z")
            continue
        pts = cmd[1:]
        xy = " ".join(f"{pts[i] + ox:.3f} {H - pts[i + 1]:.3f}" for i in range(0, len(pts), 2))
        parts.append(f"{op}{xy}")
    return "".join(parts)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def label_svg(label: Label, out: Path, style: dict, *, live_text: bool = False, bleed: bool = False) -> list[str]:
    """SVG с именованными слоями (serif:id), как в мастер-файле. Открывается/помещается в Affinity.

    live_text=True — надписи живым текстом (шрифт style.live_text_family), иначе — в кривых.
    """
    scene = layout(label, style)
    W, H = style["size"]["w"], style["size"]["h"]
    b = style["bleed"] if bleed else 0.0
    VW, VH = W + 2 * b, H + 2 * b
    font = load_font(tuple(style["fonts"]), str(ROOT))
    circ = style["circuits"][label.circuit]
    bgc = "rgb({},{},{})".format(*style["background"]["rgb"])
    fgc = "rgb({},{},{})".format(*style["text_color"]["rgb"])
    cc = "rgb({},{},{})".format(*circ["rgb"])
    k = style["kant"]

    def d(path):
        return _svg_d(transform(path, (1, 0, 0, 1, b, b)), VH)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:serif="http://www.serif.com/" '
        f'width="{VW:g}mm" height="{VH:g}mm" viewBox="0 0 {VW:g} {VH:g}">',
        f"<title>{_esc(label.title)}</title>",
        "<defs>",
        f'<linearGradient id="kant" gradientUnits="userSpaceOnUse" x1="0" y1="{VH - b - k["inset"]:.3f}" '
        f'x2="0" y2="{VH - b - H * k["fade_end"]:.3f}">'
        f'<stop offset="0" stop-color="{cc}"/><stop offset="1" stop-color="{bgc}"/></linearGradient>',
        "</defs>",
    ]
    groups: dict[str, list[str]] = {}
    for el in scene:
        kind = el["kind"]
        if kind == "meta":
            continue
        g = groups.setdefault(el["layer"], [])
        if kind == "bg":
            path = el["path"] if bleed else rounded_rect(0, 0, W, H, style["corner_radius"])
            g.append(f'<path d="{d(path)}" fill="{bgc}"/>')
        elif kind == "kant":
            g.append(f'<path d="{d(el["path"])}" fill="url(#kant)" fill-rule="evenodd"/>')
        elif kind == "text":
            if live_text:
                fs = el["cap"] * font.upm / font.cap
                ax, ay = el["anchor"]
                ax, ay = ax + b, VH - (ay + b)
                rot = f' transform="rotate(90 {ax:.3f} {ay:.3f})"' if el["rotate"] else ""
                anchor = ' text-anchor="middle"' if not el["rotate"] else ""
                g.append(f'<text x="{ax:.3f}" y="{ay:.3f}"{anchor}{rot} font-family="{_esc(style["live_text_family"])}" '
                         f'font-size="{fs:.3f}" fill="{fgc}">{_esc(el["text"])}</text>')
            else:
                g.append(f'<path serif:id="{_esc(el["text"])}" d="{d(el["path"])}" fill="{fgc}" fill-rule="evenodd"/>')
        elif kind == "cut":
            g.append(f'<path d="{d(el["path"])}" fill="none" stroke="rgb(236,0,140)" stroke-width="0.088"/>')
    for i, (name, items) in enumerate(groups.items()):
        lines.append(f'<g id="L{i}" serif:id="{_esc(name)}">' + "".join(items) + "</g>")
    lines.append("</svg>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return scene[-1]["warnings"]


def pdf_to_png(pdf: Path, png: Path, dpi: int = 200) -> Path:
    import pymupdf

    with pymupdf.open(pdf) as doc:
        pix = doc[0].get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
        png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(png)
    return png
