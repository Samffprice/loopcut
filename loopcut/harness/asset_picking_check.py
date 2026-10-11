#!/usr/bin/env python3
"""Offline picking check (C34): for twenty everyday briefs, does a library search put a right asset in the
first row of its sheet (the top four)? No model and no Blender: it calls the add-on's own search tools with a
stand-in bpy, against the live public APIs, and lays the thumbnails out the way asset_common.sheet does.

    uv run --with pillow python3 loopcut/harness/asset_picking_check.py --out <dir> [--config-dir <dir>] [ids]

--config-dir is a Loopcut config folder whose credentials.json holds the user's own keys (Poly Pizza needs
one); without it Poly Pizza is skipped and BlenderKit searches free assets only, as for a user with no keys.
Loopcut's own index needs a signed-in client and the service switched on, so it is not part of this check.

Writes, per brief: <id>.<source>.png (the sheet as the model gets it) and <id>.review.png (each source's top
row, labelled, for judging); and picking.md / picking.json with every row's text. Judge each review image:
a source hits when one of its first four is a right asset for the brief (right object, fitting style). Record
the verdicts in picking.json ("hits": {source: true/false}) and precision is printed with --score.
"""

import argparse
import json
import os
import sys
import tempfile
import traceback
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))

BRIEFS = [
    # Realistic interior
    ("int_chair", "interior", "A wooden dining chair for a farmhouse kitchen", "wooden dining chair", "model",
     ["blenderkit", "polyhaven"]),
    ("int_mug", "interior", "A ceramic coffee mug on a desk", "coffee mug", "model", ["blenderkit", "polyhaven"]),
    ("int_plant", "interior", "A potted houseplant in a living-room corner", "potted plant", "model",
     ["blenderkit", "polyhaven"]),
    ("int_armchair", "interior", "A leather armchair for a reading corner", "leather armchair", "model",
     ["blenderkit", "polyhaven"]),
    ("int_lamp", "interior", "A floor lamp with a fabric shade", "floor lamp", "model", ["blenderkit", "polyhaven"]),
    # Stylised game kit
    ("sty_chest", "stylised", "A low-poly treasure chest for a stylised dungeon", "treasure chest", "model",
     ["polypizza", "blenderkit"]),
    ("sty_pine", "stylised", "A low-poly pine tree for a stylised forest", "pine tree", "model",
     ["polypizza", "blenderkit"]),
    ("sty_barrel", "stylised", "A cartoon wooden barrel for a game tavern", "barrel", "model",
     ["polypizza", "blenderkit"]),
    ("sty_sword", "stylised", "A stylised fantasy sword prop", "sword", "model", ["polypizza", "blenderkit"]),
    # Nature and exterior
    ("ext_rock", "exterior", "A mossy boulder for a forest floor", "mossy rock", "model", ["polyhaven", "blenderkit"]),
    ("ext_bench", "exterior", "A wooden park bench", "park bench", "model", ["blenderkit", "polyhaven"]),
    ("ext_streetlamp", "exterior", "A street lamp post for a city sidewalk", "street lamp", "model",
     ["blenderkit", "polyhaven"]),
    ("ext_fern", "exterior", "Ferns for forest ground cover", "fern", "model", ["polyhaven", "blenderkit"]),
    # Materials
    ("mat_woodfloor", "material", "Oak floor planks for a living room", "wood floor", "material",
     ["polyhaven", "blenderkit"]),
    ("mat_brick", "material", "A red brick wall", "red brick", "material", ["polyhaven", "blenderkit"]),
    ("mat_metal", "material", "Brushed steel for an appliance", "brushed metal", "material",
     ["polyhaven", "blenderkit"]),
    ("mat_concrete", "material", "A polished concrete floor", "concrete floor", "material",
     ["polyhaven", "blenderkit"]),
    # Skies
    ("sky_clear", "sky", "A clear midday sky over an open field", "clear sky", "hdri", ["polyhaven", "blenderkit"]),
    ("sky_sunset", "sky", "A warm golden-hour sunset", "sunset", "hdri", ["polyhaven", "blenderkit"]),
    ("sky_studio", "sky", "Soft studio lighting for a product shot", "studio", "hdri", ["polyhaven", "blenderkit"]),
]
TILE, COLUMNS, GAP = 160, 4, 4  # asset_common.sheet's layout.


def stand_in_bpy():
    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace(driver_namespace={}, cachedir=tempfile.mkdtemp(prefix="picking-cache-"))
    bpy.context = types.SimpleNamespace(preferences=types.SimpleNamespace(addons={}))
    sys.modules.setdefault("bpy", bpy)


def make_sheet(out: Path, holder: dict):
    """asset_common.sheet with Pillow: four 160 px tiles a row, each scaled to the tile's height and cut to
    its width, blank cells dark grey. Saved under the brief and source being searched."""
    from PIL import Image

    def sheet(paths, name="sheet.png", cell=(TILE, TILE), columns=COLUMNS, folder=None, fit=False):
        rows = (len(paths) + columns - 1) // columns
        canvas = Image.new("RGB", (columns * TILE + (columns - 1) * GAP, rows * TILE + (rows - 1) * GAP),
                           (38, 38, 38))
        for index, path in enumerate(paths):
            if not path:
                continue
            try:
                image = Image.open(path).convert("RGBA")
                flat = Image.new("RGB", image.size, (38, 38, 38))  # Transparent previews over the sheet's grey.
                flat.paste(image, mask=image.getchannel("A"))
                image = flat
            except Exception as ex:  # noqa: BLE001 - a bad preview is a blank tile, as in the add-on
                print(f"  unreadable thumbnail {path}: {ex}")
                continue
            row, column = divmod(index, columns)
            if fit:  # asset_common.fit_box: whole and centred, as the search sheets are now.
                scale = min(TILE / image.width, TILE / image.height)
                w, h = max(1, min(TILE, round(image.width * scale))), max(1, min(TILE, round(image.height * scale)))
                canvas.paste(image.resize((w, h)), (column * (TILE + GAP) + (TILE - w) // 2,
                                                    row * (TILE + GAP) + (TILE - h) // 2))
                continue
            image = image.resize((max(1, round(image.width * TILE / image.height)), TILE))
            canvas.paste(image.crop((0, 0, min(TILE, image.width), TILE)), (column * (TILE + GAP), row * (TILE + GAP)))
        target = out / f"{holder['brief']}.{holder['source']}.png"
        canvas.save(target)
        holder["sheet"] = target
        return target
    return sheet


def review(out: Path, brief, sheets: dict):
    """Each source's first row under a label: the image to judge a brief by."""
    from PIL import Image, ImageDraw
    width = COLUMNS * TILE + (COLUMNS - 1) * GAP
    band = 22
    canvas = Image.new("RGB", (width, band + len(sheets) * (band + TILE)), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    draw.text((4, 4), f"{brief[0]}: {brief[2]} (query {brief[3]!r})", fill=(0, 0, 0))
    y = band
    for source, path in sheets.items():
        draw.text((4, y + 5), source, fill=(160, 0, 0))
        y += band
        if path:
            first = Image.open(path).crop((0, 0, width, TILE))
            canvas.paste(first, (0, y))
        else:
            draw.text((8, y + TILE // 2), "no results", fill=(0, 0, 0))
        y += TILE
    target = out / f"{brief[0]}.review.png"
    canvas.save(target)
    return target


def run(out: Path, only: list[str]):
    stand_in_bpy()
    from loopcut import assets, blenderkit, polyhaven, polypizza
    holder = {}
    sheet = make_sheet(out, holder)
    for module in (blenderkit, polyhaven, polypizza):
        module.sheet = sheet
    record = {}
    for brief in BRIEFS:
        brief_id, category, text, query, kind, sources = brief
        if only and brief_id not in only:
            continue
        print(brief_id)
        entry = {"category": category, "brief": text, "query": query, "type": kind, "results": {}, "hits": {}}
        sheets = {}
        for source in sources:
            if source == "polypizza" and not polypizza.api_key():
                continue
            holder.update(brief=brief_id, source=source, sheet=None)
            arguments = {"source": source, "query": query}
            if source != "polypizza":
                arguments["type"] = kind
            try:
                result = assets.search_assets(**arguments)
                entry["results"][source] = result.text
            except Exception as ex:  # noqa: BLE001 - a failed source is recorded, not fatal
                entry["results"][source] = f"FAILED: {ex}"
                traceback.print_exc(limit=2)
            sheets[source] = holder["sheet"]
        review(out, brief, sheets)
        record[brief_id] = entry
    return record


def write(out: Path, record: dict):
    previous = {}
    path = out / "picking.json"
    if path.exists():
        previous = json.loads(path.read_text())
    for brief_id, entry in record.items():  # Verdicts already recorded stay.
        entry.update({k: v for k, v in previous.get(brief_id, {}).items() if k in ("hits", "primary", "note")})
    path.write_text(json.dumps({**previous, **record}, indent=1))
    lines = ["# Offline picking check", ""]
    for brief_id, entry in {**previous, **record}.items():
        lines += [f"## {brief_id} ({entry['category']}): {entry['brief']}", "",
                  f"Query {entry['query']!r}, type {entry['type']}. Review image: `{brief_id}.review.png`.", ""]
        for source, text in entry["results"].items():
            lines += [f"### {source}", "", "```", text, "```", ""]
    (out / "picking.md").write_text("\n".join(lines))


def score(out: Path):
    record = json.loads((out / "picking.json").read_text())
    by_source, by_category, any_hit = {}, {}, 0
    for entry in record.values():
        hits = entry.get("hits", {})
        for source, hit in hits.items():
            by_source.setdefault(source, []).append(bool(hit))
        best = any(hits.values())
        any_hit += best
        by_category.setdefault(entry["category"], []).append(best)
    print(f"briefs: {len(record)}; a right asset in some source's top four: {any_hit}/{len(record)}")
    for name, values in [*by_source.items(), *by_category.items()]:
        print(f"  {name}: {sum(values)}/{len(values)} = {sum(values) / len(values):.0%}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ids", nargs="*", help="brief ids (default all)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, help="a Loopcut config folder with the user's own keys")
    parser.add_argument("--score", action="store_true", help="print precision from the verdicts in picking.json")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.score:
        score(args.out)
        return
    os.environ["LOOPCUT_DATA_DIR"] = tempfile.mkdtemp(prefix="picking-data-")  # thumbnails and catalogues
    os.environ["LOOPCUT_CONFIG_DIR"] = str(args.config_dir or tempfile.mkdtemp(prefix="picking-config-"))
    write(args.out, run(args.out, args.ids))


if __name__ == "__main__":
    main()
