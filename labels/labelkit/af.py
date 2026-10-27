"""Чтение файлов Affinity (.af, версия 3.x) без запуска Affinity.

Формат .af закрытый. Здесь разобрана только та часть, которая нужна библиотеке
маркировки, и только на чтение:

* метаданные (название документа, версия Affinity) — JSON в конце файла;
* миниатюра PNG, которую Affinity кладёт в файл;
* размер документа (px + DPI документа -> мм);
* дерево слоёв: имена групп, видимость, текстовые слои и их содержимое.

Документ хранится в zstd-кадре. Внутри — поток тегов, записанных задом
наперёд (``+cseD`` = Desc, ``+8ftU`` = Utf8, ``)isiV`` = Visi, ``dlhC`` = Chld).
Узел-группа пишется как Desc, Visi, Chld(n) и n дочерних узлов; текстовый слой —
сначала содержимое (Utf8), потом свой Desc и Visi.

Записывать .af этот модуль не умеет и не должен: изменения в мастер-файл
вносятся в самой Affinity (агент готовит для этого SVG/PDF).
"""
from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

import zstandard

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
KANT_RE = re.compile(r"^(ГВС|ХВС|РЕЦ|[А-ЯA-Z]{2,6})\s+.*КАНТ", re.I)


@dataclass
class Node:
    name: str
    visible: bool
    text: str = ""
    children: list["Node"] = field(default_factory=list)

    @property
    def is_text(self) -> bool:
        return bool(self.text)

    def texts(self) -> list[str]:
        """Все тексты внутри узла (сам узел + потомки) по порядку."""
        out = [self.text] if self.text else []
        for c in self.children:
            out += c.texts()
        return out


@dataclass
class AfDocument:
    path: Path
    title: str
    client_version: str
    size_px: tuple[float, float] | None
    dpi: float | None
    fonts: list[str]
    root: Node | None
    thumbnail_png: bytes | None

    @property
    def size_mm(self) -> tuple[float, float] | None:
        if not self.size_px or not self.dpi:
            return None
        k = 25.4 / self.dpi
        return round(self.size_px[0] * k, 2), round(self.size_px[1] * k, 2)


def _decompress_doc(raw: bytes) -> bytes:
    """Возвращает распакованный doc.dat — самый большой кадр с тегами слоёв."""
    best = b""
    dctx = zstandard.ZstdDecompressor()
    start = raw.find(ZSTD_MAGIC)
    while start >= 0:
        try:
            data = dctx.decompressobj().decompress(raw[start:])
            if b"+cseD" in data and len(data) > len(best):
                best = data
        except zstandard.ZstdError:
            pass
        start = raw.find(ZSTD_MAGIC, start + 4)
    return best


def _events(doc: bytes) -> list[tuple]:
    pat = b"|".join(re.escape(t) for t in (b"+cseD", b"+8ftU", b")isiV", b"dlhC"))
    out = []
    for m in re.finditer(pat, doc):
        tag, i = m.group(0), m.end()
        if tag == b")isiV":
            out.append(("V", doc[i]))
        elif tag == b"dlhC":
            out.append(("C", struct.unpack("<I", doc[i:i + 4])[0]))
        else:
            n = struct.unpack("<I", doc[i:i + 4])[0]
            if n > 2000:
                continue
            s = doc[i + 4:i + 4 + n].decode("utf-8", "replace")
            if tag == b"+cseD":
                out.append(("D", s))
            # содержимое текстового слоя заканчивается \0; служебные строки стилей — нет
            elif s.endswith("\x00") and s.strip("\x00").strip():
                out.append(("T", s.rstrip("\x00").strip()))
    return out


def _parse_tree(ev: list[tuple]) -> Node | None:
    pos = 0

    def node() -> Node:
        nonlocal pos
        texts = []
        while ev[pos][0] == "T":
            texts.append(ev[pos][1])
            pos += 1
        if ev[pos][0] != "D":
            raise ValueError(f"ожидался Desc, получено {ev[pos]}")
        n = Node(name=ev[pos][1], visible=True, text=" ".join(texts))
        pos += 1
        if pos < len(ev) and ev[pos][0] == "V":
            n.visible = bool(ev[pos][1])
            pos += 1
        if pos < len(ev) and ev[pos][0] == "C":
            count = ev[pos][1]
            pos += 1
            for _ in range(count):
                n.children.append(node())
        return n

    try:
        return node()
    except (IndexError, ValueError):
        return None


def read_af(path: str | Path) -> AfDocument:
    path = Path(path)
    raw = path.read_bytes()

    meta = {}
    m = re.search(rb'\{"document":\{.*?\}\}', raw, re.S)
    if m:
        meta = json.loads(m.group(0).decode("utf-8", "replace"))["document"]

    thumb = None
    i = raw.find(b"\x89PNG\r\n\x1a\n")
    if i >= 0:
        j = raw.find(b"IEND", i)
        thumb = raw[i:j + 8] if j > 0 else None

    doc = _decompress_doc(raw)
    size_px = dpi = None
    k = doc.find(b"stcr")
    if k >= 0:
        seg = doc[k + 4:k + 400]
        # первые ненулевые double после тега — ширина и высота страницы в px
        for o in range(0, len(seg) - 16):
            w, h = struct.unpack("<dd", seg[o:o + 16])
            if 1 < w < 1e6 and 1 < h < 1e6 and seg[o:o + 8] != b"\x00" * 8:
                size_px = (w, h)
                break
    k = doc.find(b"IPPU")  # UPPI — units per inch (DPI документа)
    if k >= 0:
        dpi = struct.unpack("<d", doc[k + 4:k + 12])[0]

    fonts = sorted({f.decode() for f in re.findall(rb"tsoP.\x00\x00\x00([\x21-\x2a\x2c-\x7e]{2,60})\+", doc)})

    return AfDocument(
        path=path,
        title=meta.get("title", path.stem),
        client_version=meta.get("clientVersion", ""),
        size_px=size_px,
        dpi=dpi,
        fonts=fonts,
        root=_parse_tree(_events(doc)) if doc else None,
        thumbnail_png=thumb,
    )


# ---------------------------------------------------------------- анализ дерева

def _layer_nodes(root: Node) -> list[Node]:
    """Узлы слоя: спуск через безымянные обёртки (разворот -> слой)."""
    n = root
    while len(n.children) == 1 and not n.children[0].name and not n.children[0].is_text:
        n = n.children[0]
    return n.children


def _items(group: Node) -> list[tuple[str, bool]]:
    """Варианты надписей в группе: (текст, видим). Подгруппа = многострочная надпись."""
    out = []
    for c in group.children:
        if c.is_text:
            out.append((c.text, c.visible))
        elif c.children:
            if c.name and not KANT_RE.match(c.name):
                out.append((" ".join(c.texts()), c.visible))
            else:
                out += [(t, v and c.visible) for t, v in _items(c)]
    return out


def library(doc: AfDocument) -> dict:
    """Словарь библиотеки из мастер-файла + какая наклейка сейчас «собрана» (видимые слои)."""
    cats: dict[str, list[str]] = {}
    visible: dict[str, list[str]] = {}
    circuits_all, circuit_visible = [], []
    if not doc.root:
        return {"categories": cats, "visible": visible, "circuits": [], "circuit": None}

    def collect(g: Node, parent_visible: bool, cat: str):
        vis = parent_visible and g.visible
        for text, v in _items(g):
            lst = cats.setdefault(cat, [])
            if text not in lst:
                lst.append(text)
            if vis and v:
                visible.setdefault(cat, []).append(text)

    for g in _layer_nodes(doc.root):
        if g.is_text:  # свободные надписи на слое (РАДИАТОР, КОНВЕКТОР...) — отопление
            wrapper = Node(name="ОТОПЛЕНИЕ", visible=True, children=[g])
            collect(wrapper, True, "ОТОПЛЕНИЕ")
            continue
        if not g.name:
            continue
        km = KANT_RE.match(g.name)
        if km:
            circuits_all.append(km.group(1).upper())
            if g.visible:
                circuit_visible.append(km.group(1).upper())
            continue
        sub = [c for c in g.children if c.name and c.children and not c.is_text]
        if sub and not any(c.is_text for c in g.children):
            for s in sub:  # «ВЕРХНИЕ НАДПИСИ» -> строки
                collect(s, g.visible, s.name)
        elif any(c.is_text or c.children for c in g.children):
            collect(g, True, g.name)

    return {
        "categories": cats,
        "visible": visible,
        "circuits": circuits_all,
        "circuit": circuit_visible[0] if circuit_visible else None,
    }


def dump_tree(node: Node, indent: int = 0) -> str:
    mark = "👁" if node.visible else "·"
    label = node.name or ("«" + node.text + "»" if node.text else "(без имени)")
    lines = ["  " * indent + f"{mark} {label}"]
    for c in node.children:
        lines.append(dump_tree(c, indent + 1))
    return "\n".join(lines)
