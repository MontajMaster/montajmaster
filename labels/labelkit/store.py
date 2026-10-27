"""База библиотеки маркировки.

* db/labels.csv      — реестр наклеек (одна строка = один уникальный макет). Открывается в Excel.
* db/dictionary.json — словарь надписей по слоям мастер-файла Affinity + контуры.
* db/orders/*.csv    — заказы (объекты): какие наклейки и сколько штук печатать.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path

from .render import ROOT, Label

DB = ROOT / "db"
LABELS_CSV = DB / "labels.csv"
DICT_JSON = DB / "dictionary.json"
FIELDS = ["id", "circuit", "top", "top2", "small", "main", "template", "status", "source", "created", "note"]

# какому слою мастер-файла соответствует поле наклейки
ROLE_LAYERS = {
    "top": ["ВЕРХНЯЯ СТРОКА"],
    "top2": ["НИЖНЯЯ СТРОКА"],
    "small": ["МАЛАЯ СТРОКА НИЗ"],
    "main": ["ПОТРЕБИТЕЛИ ВОДА", "ОТОПЛЕНИЕ"],
}


def norm(s: str) -> str:
    """Ключ сравнения: без регистра, точек, пробелов и «|»."""
    return re.sub(r"[\s.|]+", "", s or "").upper().replace("Ё", "Е")


# ------------------------------------------------------------------ словарь

def load_dict() -> dict:
    if DICT_JSON.exists():
        return json.loads(DICT_JSON.read_text(encoding="utf-8"))
    return {"categories": {}, "circuits": []}


def save_dict(d: dict) -> None:
    DB.mkdir(parents=True, exist_ok=True)
    DICT_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def merge_dict(d: dict, categories: dict[str, list[str]], circuits: list[str]) -> list[str]:
    added = []
    for cat, items in categories.items():
        lst = d["categories"].setdefault(cat, [])
        known = {norm(x) for x in lst}
        for it in items:
            if norm(it) not in known:
                lst.append(it)
                known.add(norm(it))
                added.append(f"{cat}: {it}")
    for c in circuits:
        if c not in d["circuits"]:
            d["circuits"].append(c)
    return added


def canonical(d: dict, role: str, text: str) -> str | None:
    """Написание из словаря для поля (КУХ МОЙКА -> КУХ.МОЙКА) или None, если слова нет."""
    if not text:
        return ""
    key = norm(text)
    for cat in ROLE_LAYERS[role]:
        for it in d["categories"].get(cat, []):
            if norm(it) == key:
                return it.replace(" ", "|") if role == "main" and "|" in text else it
    return None


# ------------------------------------------------------------------ реестр наклеек

def load_labels() -> list[dict]:
    if not LABELS_CSV.exists():
        return []
    with LABELS_CSV.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_labels(rows: list[dict]) -> None:
    DB.mkdir(parents=True, exist_ok=True)
    with LABELS_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def key_of(r: dict) -> tuple:
    return tuple(norm(r.get(k, "")) for k in ("circuit", "top", "top2", "small", "main"))


def next_id(rows: list[dict]) -> str:
    n = max((int(r["id"][1:]) for r in rows if re.fullmatch(r"L\d+", r.get("id", ""))), default=0)
    return f"L{n + 1:04d}"


def upsert(rows: list[dict], label: Label, **extra) -> tuple[dict, bool]:
    """Добавляет наклейку, если такой ещё нет. -> (строка, создана_ли)."""
    rec = {"circuit": label.circuit, "top": label.top, "top2": label.top2, "small": label.small, "main": label.main}
    k = key_of(rec)
    for r in rows:
        if key_of(r) == k:
            for f, v in extra.items():
                if f in ("source", "template") and v and not r.get(f):
                    r[f] = v
            return r, False
    rec.update(id=next_id(rows), template=extra.get("template", "pipe-32x62"), status=extra.get("status", "new"),
               source=extra.get("source", ""), created=dt.date.today().isoformat(), note=extra.get("note", ""))
    rows.append(rec)
    return rec, True


def to_label(r: dict) -> Label:
    return Label(circuit=r["circuit"], top=r.get("top", ""), top2=r.get("top2", ""),
                 small=r.get("small", ""), main=r.get("main", ""), id=r.get("id", ""))


def find(rows: list[dict], ids: list[str]) -> list[dict]:
    by = {r["id"]: r for r in rows}
    missing = [i for i in ids if i not in by]
    if missing:
        raise KeyError("Нет в базе: " + ", ".join(missing))
    return [by[i] for i in ids]


# ------------------------------------------------------------------ заказы

ORDER_FIELDS = ["circuit", "top", "top2", "small", "main", "qty", "id"]


def read_order(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        return [r for r in csv.DictReader(f, dialect=dialect) if any((v or "").strip() for v in r.values())]
