"""labelkit — командная строка библиотеки маркировки MontajMaster.

Запуск из корня репозитория:  python3 labels/lk.py <команда> ...
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import af, store
from .render import ROOT, Label, label_pdf, label_svg, load_style, pdf_to_png
from .sheet import build, plan

OUT = ROOT / "out"


def safe(s: str) -> str:
    return "".join(ch for ch in s if ch not in '<>:"/\\|?*').strip() or "_"


def _print_rows(rows):
    if not rows:
        print("(пусто)")
        return
    print(f"{'id':6} {'контур':5} {'верх':14} {'верх-2':12} {'малая':12} {'надпись':22} статус")
    for r in rows:
        print(f"{r['id']:6} {r['circuit']:5} {r['top']:14} {r['top2']:12} {r['small']:12} {r['main']:22} {r['status']}")


def _resolve(d: dict, role: str, text: str, learn: bool, errors: list[str]) -> str:
    if not text:
        return ""
    canon = store.canonical(d, role, text)
    if canon is not None:
        return canon
    if learn:
        cat = store.ROLE_LAYERS[role][0]
        store.merge_dict(d, {cat: [text.replace("|", " ") if role != "main" else text]}, [])
        return text
    errors.append(f"«{text}» нет в словаре слоя {store.ROLE_LAYERS[role][0]} (добавить: --learn)")
    return text


def _make_labels(d, style, circuits, top, top2, small, main, learn) -> list[Label]:
    errors: list[str] = []
    vals = {
        "top": _resolve(d, "top", top, learn, errors),
        "top2": _resolve(d, "top2", top2, learn, errors),
        "small": _resolve(d, "small", small, learn, errors),
        "main": _resolve(d, "main", main, learn, errors),
    }
    out = []
    for c in [x.strip().upper() for x in circuits.split(",") if x.strip()]:
        if c not in style["circuits"]:
            errors.append(f"контур «{c}» не описан в config/style.json (есть: {', '.join(style['circuits'])})")
            continue
        out.append(Label(circuit=c, **vals))
    if errors:
        raise SystemExit("Ошибка:\n  " + "\n  ".join(errors))
    return out


# ------------------------------------------------------------------ команды

def cmd_af_info(a):
    for p in a.files:
        doc = af.read_af(p)
        lib = af.library(doc)
        print(f"== {doc.title}  ({Path(p).name})")
        print(f"   Affinity {doc.client_version}, размер {doc.size_mm} мм @ {doc.dpi:g} DPI, шрифты: {', '.join(doc.fonts)}")
        print(f"   собранная наклейка: контур={lib['circuit']}  видимые надписи={json.dumps(lib['visible'], ensure_ascii=False)}")
        if a.tree and doc.root:
            print(af.dump_tree(doc.root))
        if a.json:
            print(json.dumps(lib, ensure_ascii=False, indent=2))
        if a.thumb and doc.thumbnail_png:
            t = Path(a.thumb) / (safe(doc.title) + ".png")
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(doc.thumbnail_png)
            print(f"   миниатюра: {t}")


def cmd_af_import(a):
    d = store.load_dict()
    rows = store.load_labels()
    for p in a.files:
        doc = af.read_af(p)
        lib = af.library(doc)
        added = store.merge_dict(d, lib["categories"], lib["circuits"])
        vis = lib["visible"]

        def pick(cat):
            return " ".join(vis.get(cat, []))

        main_items = vis.get("ПОТРЕБИТЕЛИ ВОДА", []) + vis.get("ОТОПЛЕНИЕ", [])
        label = Label(circuit=lib["circuit"] or "?", top=pick("ВЕРХНЯЯ СТРОКА"), top2=pick("НИЖНЯЯ СТРОКА"),
                      small=pick("МАЛАЯ СТРОКА НИЗ"), main="|".join(main_items))
        src = Path(p)
        if not a.no_copy:
            dest = ROOT / "affinity" / safe(label.top or "_") / (safe(doc.title) + ".af")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
            src = dest
        rel = str(src.relative_to(ROOT)) if src.resolve().is_relative_to(ROOT) else str(src)
        rec, created = store.upsert(rows, label, source=rel, status="affinity",
                                    template=f"pipe-{round(doc.size_mm[0])}x{round(doc.size_mm[1])}" if doc.size_mm else "")
        if doc.thumbnail_png:
            (ROOT / "previews").mkdir(exist_ok=True)
            (ROOT / "previews" / f"{rec['id']}.png").write_bytes(doc.thumbnail_png)
        print(f"{'+' if created else '='} {rec['id']}  {doc.title}  ->  {label.circuit} | {label.top} | {label.main}  [{rel}]")
        for x in added:
            print(f"    словарь + {x}")
    store.save_dict(d)
    store.save_labels(rows)


def cmd_dict(a):
    d = store.load_dict()
    print("Контуры:", ", ".join(d.get("circuits", [])))
    for cat, items in d["categories"].items():
        if a.cat and a.cat.upper() not in cat.upper():
            continue
        print(f"\n[{cat}] ({len(items)})")
        print("  " + " · ".join(items))


def cmd_add(a):
    style = load_style()
    d = store.load_dict()
    rows = store.load_labels()
    labels = _make_labels(d, style, a.circuit, a.top or "", a.top2 or "", a.small or "", a.main, a.learn)
    for lab in labels:
        rec, created = store.upsert(rows, lab, note=a.note or "")
        print(f"{'+ создана' if created else '= уже есть'} {rec['id']}  {lab.title}")
    store.save_labels(rows)
    if a.learn:
        store.save_dict(d)


def cmd_list(a):
    rows = store.load_labels()
    if a.circuit:
        rows = [r for r in rows if r["circuit"] == a.circuit.upper()]
    if a.status:
        rows = [r for r in rows if r["status"] == a.status]
    if a.search:
        q = store.norm(a.search)
        rows = [r for r in rows if q in store.norm(" ".join(r[k] for k in ("top", "top2", "small", "main")))]
    _print_rows(rows)
    print(f"\nвсего: {len(rows)}")


def cmd_status(a):
    rows = store.load_labels()
    for r in store.find(rows, a.ids.split(",")):
        r["status"] = a.value
        print(f"{r['id']} -> {a.value}")
    store.save_labels(rows)


def _select(a, rows):
    if getattr(a, "all", False):
        return rows
    if getattr(a, "status_filter", None):
        return [r for r in rows if r["status"] == a.status_filter]
    if not a.ids:
        raise SystemExit("Укажите id через запятую, --all или --status")
    return store.find(rows, a.ids.split(","))


def cmd_render(a):
    style = load_style()
    rows = store.load_labels()
    sel = _select(a, rows)
    fmts = set(a.format.split(","))
    for r in sel:
        lab = store.to_label(r)
        base = OUT / "labels" / safe(lab.top or "_") / f"{r['id']} {safe(lab.title)}"
        warn = []
        if "pdf" in fmts or "png" in fmts:
            warn = label_pdf(lab, base.with_suffix(".pdf"), style, cmyk=not a.rgb)
        if "png" in fmts:
            tmp = base.with_suffix(".rgb.pdf")
            label_pdf(lab, tmp, style, cmyk=False, cut=False)
            pdf_to_png(tmp, base.with_suffix(".png"), dpi=a.dpi)
            tmp.unlink()
            if "pdf" not in fmts:
                base.with_suffix(".pdf").unlink()
        if "svg" in fmts:
            warn = label_svg(lab, base.with_suffix(".svg"), style, live_text=a.live_text, bleed=a.bleed)
        print(f"{r['id']}  {base.relative_to(ROOT)}.{{{','.join(sorted(fmts))}}}")
        for w in warn:
            print(f"   ! {w}")


def _run_sheet(items, a, job):
    style = load_style()
    fmt = a.sheet or style["sheet_defaults"]["format"]
    out = OUT / "print" / safe(job) / f"{safe(job)} {fmt}.pdf"
    summary = build(items, out, style, fmt=fmt, margin=a.margin, gap=a.gap, cut=not a.no_cut, job=job)
    import pymupdf

    with pymupdf.open(out) as doc:
        for i, page in enumerate(doc):
            png = out.with_name(f"{out.stem} превью {i + 1}.png")
            page.get_pixmap(dpi=a.preview_dpi, colorspace=pymupdf.csRGB).save(png)
    lines = [
        f"Задание: {job}",
        f"Файл: {Path(summary['pdf']).relative_to(ROOT)}",
        f"Формат листа: {summary['format']}, раскладка {summary['grid']}, {summary['per_sheet']} шт/лист",
        f"Листов: {summary['sheets']}, наклеек: {summary['total']} (уникальных макетов: {summary['unique']}), "
        f"заполнение: {summary['fill']}%",
        "По контурам: " + ", ".join(f"{k} — {v}" for k, v in summary["by_circuit"].items()),
        f"Печать CMYK, вылет {style['bleed']} мм, контур реза — плашка «{style['cut']['spot_name']}» (overprint)"
        if not a.no_cut else "Без контура реза",
        "",
        "Состав:",
    ] + [f"  {lab.id or '-':6} {lab.title:40} x{q}" for lab, q in items]
    if summary["warnings"]:
        lines += ["", "Предупреждения:"] + [f"  ! {w}" for w in summary["warnings"]]
    (out.parent / "сводка.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nПревью: {out.parent.relative_to(ROOT)}/")


def cmd_sheet(a):
    rows = store.load_labels()
    sel = _select(a, rows)
    items = [(store.to_label(r), a.qty) for r in sel]
    _run_sheet(items, a, a.job or "лист")


def cmd_order(a):
    style = load_style()
    d = store.load_dict()
    rows = store.load_labels()
    path = Path(a.file)
    items = []
    for line in store.read_order(path):
        qty = int((line.get("qty") or "1").strip() or 1)
        if (line.get("id") or "").strip():
            r = store.find(rows, [line["id"].strip()])[0]
            items.append((store.to_label(r), qty))
            continue
        labels = _make_labels(d, style, line.get("circuit", ""), (line.get("top") or "").strip(),
                              (line.get("top2") or "").strip(), (line.get("small") or "").strip(),
                              (line.get("main") or "").strip(), a.learn)
        for lab in labels:
            rec, created = store.upsert(rows, lab, note=f"заказ {path.stem}")
            lab.id = rec["id"]
            if created:
                print(f"+ в библиотеку: {rec['id']} {lab.title}")
            items.append((lab, qty))
    store.save_labels(rows)
    if a.learn:
        store.save_dict(d)
    _run_sheet(items, a, a.job or path.stem)


def cmd_plan(a):
    style = load_style()
    for fmt in style["sheets"]:
        g = plan(style, fmt, a.margin if a.margin is not None else style["sheet_defaults"]["margin"],
                 a.gap if a.gap is not None else style["sheet_defaults"]["gap"])
        print(f"{fmt:6} {g.cols}x{g.rows}{' (повёрнуты)' if g.rotated else ''} = {g.per_sheet} шт/лист")


def main(argv=None):
    p = argparse.ArgumentParser(prog="lk", description="Библиотека маркировки MontajMaster")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("af-info", help="разобрать файл(ы) Affinity .af")
    s.add_argument("files", nargs="+")
    s.add_argument("--tree", action="store_true", help="показать дерево слоёв")
    s.add_argument("--json", action="store_true", help="словарь надписей в JSON")
    s.add_argument("--thumb", metavar="DIR", help="сохранить миниатюру PNG в папку")
    s.set_defaults(func=cmd_af_info)

    s = sub.add_parser("af-import", help="занести .af в библиотеку: словарь + реестр + копия в labels/affinity")
    s.add_argument("files", nargs="+")
    s.add_argument("--no-copy", action="store_true", help="не копировать .af в labels/affinity/")
    s.set_defaults(func=cmd_af_import)

    s = sub.add_parser("dict", help="показать словарь надписей")
    s.add_argument("--cat", help="фильтр по слою")
    s.set_defaults(func=cmd_dict)

    s = sub.add_parser("add", help="добавить наклейку(и) в библиотеку")
    s.add_argument("--circuit", required=True, help="ГВС / ХВС / РЕЦ, можно списком: ГВС,ХВС,РЕЦ")
    s.add_argument("--top", help="верхняя строка (помещение)")
    s.add_argument("--top2", help="нижняя строка верхнего блока")
    s.add_argument("--small", help="малая строка (этаж, сторона)")
    s.add_argument("--main", required=True, help="основная надпись (потребитель); две строки через |")
    s.add_argument("--learn", action="store_true", help="новые слова добавить в словарь")
    s.add_argument("--note")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("list", help="реестр наклеек")
    s.add_argument("--circuit")
    s.add_argument("--status")
    s.add_argument("--search")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("status", help="сменить статус: new / affinity / approved / printed / archived")
    s.add_argument("ids")
    s.add_argument("value")
    s.set_defaults(func=cmd_status)

    def selection(s):
        s.add_argument("ids", nargs="?", help="id через запятую: L0001,L0004")
        s.add_argument("--all", action="store_true")
        s.add_argument("--status", dest="status_filter")

    s = sub.add_parser("render", help="макеты наклеек: pdf (печать), svg (в Affinity), png (превью)")
    selection(s)
    s.add_argument("--format", default="pdf,svg,png")
    s.add_argument("--live-text", action="store_true", help="SVG с живым текстом (шрифт Buyan) вместо кривых")
    s.add_argument("--bleed", action="store_true", help="SVG с вылетами")
    s.add_argument("--rgb", action="store_true", help="PDF в RGB вместо CMYK")
    s.add_argument("--dpi", type=int, default=300)
    s.set_defaults(func=cmd_render)

    def sheet_opts(s):
        s.add_argument("--sheet", help="A4 / A3 / SRA3 / SRA3+ (см. config/style.json)")
        s.add_argument("--margin", type=float)
        s.add_argument("--gap", type=float)
        s.add_argument("--no-cut", action="store_true", help="без контура реза")
        s.add_argument("--job", help="имя задания (папка в out/print)")
        s.add_argument("--preview-dpi", type=int, default=60)

    s = sub.add_parser("sheet", help="печатный лист из наклеек библиотеки")
    selection(s)
    s.add_argument("--qty", type=int, default=1, help="тираж каждой")
    sheet_opts(s)
    s.set_defaults(func=cmd_sheet)

    s = sub.add_parser("order", help="заказ из CSV (db/orders/*.csv) -> библиотека + листы под печать")
    s.add_argument("file")
    s.add_argument("--learn", action="store_true")
    sheet_opts(s)
    s.set_defaults(func=cmd_order)

    s = sub.add_parser("plan", help="сколько наклеек помещается на каждый формат листа")
    s.add_argument("--margin", type=float)
    s.add_argument("--gap", type=float)
    s.set_defaults(func=cmd_plan)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    sys.exit(main())
