"""Build step — upstream faces in, ``board_report_fonts.py`` out.

**Not imported at runtime.** ``fontTools`` and ``brotli`` are build-time only;
the renderer imports the generated module and nothing else, so the served
document carries the faces as data URIs rather than making them. Run this only
when the repertoire, the face list or the upstream pin changes:

    cd backend
    .venv/Scripts/python -m pip install "fonttools[woff]" brotli
    .venv/Scripts/python "agents/Marketing Research agent/marketing_research_agent/fonts/build_embedded.py" --src <dir>

``--src`` is a directory holding the upstream files named below. They come from
the Google Fonts repository, pinned so the build is reproducible:

    https://raw.githubusercontent.com/google/fonts/{PIN}/ofl/{family}/{file}

with ``PIN`` = :data:`UPSTREAM_COMMIT`. Each family directory there carries its
``OFL.txt`` beside the binaries; those are copied to ``<Family>-OFL.txt`` here,
which is what the SIL Open Font License requires to travel with a
redistribution. The copyright line of each family is also carried into the
emitted ``@font-face`` block as a CSS comment, so it survives being emailed as
a single HTML file with nothing else attached.

**Why these five faces and not more.** The face list is measured off the
stylesheet, not guessed. Every weight the document actually renders:

  ===============  ==========================================================
  Fraunces         600 (``h1,h2,h3``, ``.card .v``); 600 italic (``.cover h1 em``)
  Inter            400 body, 500 (``.gtable`` first column), 600 (``.absent``
                   inside ``.note``), 700 (``<b>``/``<strong>``)
  IBM Plex Mono    400 everywhere, 500 (``table.cmp thead th``), 600 (the
                   ledger's whole emphasis set), 700 (``.ins li::before``)
  ===============  ==========================================================

Inter ships as ONE variable file covering 400-700, which is both smaller than
two static instances and covers two more weights than they would. Plex Mono has
no variable upstream here, so 400 and 600 are embedded and 500/700 resolve to
them by CSS weight matching with no visible loss at 9-11px. Fraunces italic IS
shipped: it backs only the cover heading's ``<em>``, but that is a gold accent in
the approved design and a synthesised oblique of a high-contrast serif is a
slant rather than an italic. Sizes for every candidate, shipped or not, are
printed by ``--measure``.

Optical size is pinned rather than kept as an axis: retaining it costs more in
base64 than the whole Plex Mono pair, for a refinement, on a file that is
emailed. Fraunces is pinned at 28 (its rendered range is 17-60px, weighted to
the 20-31px headings and 25px card values) and Inter at its own default of 14
(the only sans sizes are 13-17px body text), which is what Google Fonts serves
by default anyway.
"""
from __future__ import annotations

import argparse
import base64
import io
import pathlib
import sys

#: google/fonts commit the upstream files were taken from. Bump only together
#: with a re-fetch, or the provenance recorded in the output becomes a fiction.
UPSTREAM_COMMIT = "5e35378e6bda803962ee6fd257e444a7d459660d"

HERE = pathlib.Path(__file__).resolve().parent
OUT_MODULE = HERE.parent / "board_report_fonts.py"

#: The report's repertoire. The rendered documents use 98 distinct characters,
#: ten of them non-ASCII (``· ÷ Δ – — ' " " → −``); the rest of this is margin
#: for prose that has not been written yet. Latin-1 is kept deliberately: an
#: accented name is the one thing likely to appear in an LLM-written sentence,
#: and a name that changes face mid-word on a client-facing page is worse than
#: the ~20% these 96 codepoints cost.
CODEPOINTS: tuple[int, ...] = tuple(sorted(set(
    list(range(0x20, 0x7F))                        # ASCII printable
    + list(range(0xA0, 0x100))                     # Latin-1 letters and symbols
    + [0x0394, 0x2206]                             # Delta - the ledger's own header
    + [0x2010, 0x2013, 0x2014]                     # hyphen, en dash, em dash
    + [0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2026, 0x2030]
    + [0x2032, 0x2033, 0x2039, 0x203A]
    + [0x20AC, 0x20B9]                             # euro, rupee
    + [0x2190, 0x2191, 0x2192, 0x2193]
    + [0x2212, 0x2248, 0x2260, 0x2264, 0x2265]
    + [0x25B2, 0x25BC]                             # the rise / fall marks the CSS injects
)))

#: OpenType features kept. ``tnum`` is not optional here — the stylesheet asks
#: for it by name so figures stay column-aligned down every money column.
FEATURES = ("kern", "liga", "clig", "calt", "ccmp", "locl", "rlig", "mark",
            "mkmk", "tnum", "zero", "frac")

#: ``(css family, css font-style, upstream file, pinned axes, css font-weight,
#: licence stem)``. ``axes`` maps an axis to a value, or to a ``(min, max)``
#: range that stays variable. The CSS family names must match the stacks in
#: ``board_report_render`` exactly, or the embedded face is never selected.
FACES: tuple[tuple[str, str, str, dict, str, str], ...] = (
    ("Fraunces", "normal", "Fraunces[SOFT,WONK,opsz,wght].ttf",
     {"wght": 600, "opsz": 28, "SOFT": 0, "WONK": 1}, "600", "Fraunces"),
    # The cover heading's ``<em>`` is a gold italic accent in the approved
    # design. Pinned to one optical size it costs a third of what the first
    # measurement (which kept the opsz axis) suggested, and synthetic oblique on
    # a high-contrast serif is a slant, not an italic.
    ("Fraunces", "italic", "Fraunces-Italic[SOFT,WONK,opsz,wght].ttf",
     {"wght": 600, "opsz": 28, "SOFT": 0, "WONK": 1}, "600", "Fraunces"),
    ("Inter", "normal", "Inter[opsz,wght].ttf",
     {"wght": (400, 700), "opsz": 14}, "400 700", "Inter"),
    ("IBM Plex Mono", "normal", "IBMPlexMono-Regular.ttf", {}, "400", "IBMPlexMono"),
    ("IBM Plex Mono", "normal", "IBMPlexMono-SemiBold.ttf", {}, "600", "IBMPlexMono"),
)

#: ``(licence stem, upstream family directory)`` — the OFL text copied in beside
#: the binaries, one per family rather than one per face.
LICENCES = (("Fraunces", "fraunces"), ("Inter", "inter"),
            ("IBMPlexMono", "ibmplexmono"))

#: Everything ``--measure`` prices, including the faces deliberately NOT shipped,
#: so the reason for leaving one out stays checkable rather than remembered.
MEASURE_ONLY: tuple[tuple[str, str, dict], ...] = (
    ("Fraunces 600, opsz axis KEPT", "Fraunces[SOFT,WONK,opsz,wght].ttf",
     {"wght": 600, "SOFT": 0, "WONK": 1}),
    ("Inter 400 static", "Inter[opsz,wght].ttf", {"wght": 400, "opsz": 14}),
    ("Inter 600 static", "Inter[opsz,wght].ttf", {"wght": 600, "opsz": 14}),
    ("IBM Plex Mono 500 (not shipped)", "IBMPlexMono-Medium.ttf", {}),
    ("IBM Plex Mono 700 (not shipped)", "IBMPlexMono-Bold.ttf", {}),
)


def _reload(font):
    """Instancing leaves ``gvar`` lazy and short a ``.notdef`` entry, which the
    subsetter then trips over. A save/reload rebuilds the tables eagerly.

    The timestamp is frozen HERE and not only on the final save: this
    intermediate write is what was stamping ``head.modified`` with the wall
    clock, which then rode through the subset and made two identical builds
    differ by a few dozen bytes.
    """
    from fontTools.ttLib import TTFont

    font.recalcTimestamp = False
    buf = io.BytesIO()
    font.save(buf)
    buf.seek(0)
    return TTFont(buf)


def build_face(src: pathlib.Path, axes: dict) -> tuple[bytes, str, list[int]]:
    """Subset one upstream file to the repertoire. Returns the woff2 bytes, the
    font's own copyright string, and the codepoints it simply does not have."""
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    font = TTFont(src)
    copyright_line = (font["name"].getDebugName(0) or "").strip()
    missing = [cp for cp in CODEPOINTS if cp not in set(font.getBestCmap())]
    if "fvar" in font:
        font = _reload(instancer.instantiateVariableFont(
            font, axes, updateFontNames=False, inplace=False))

    opts = subset.Options()
    opts.layout_features = list(FEATURES)
    opts.name_IDs = [0, 1, 2, 3, 4, 5, 6, 13, 14]      # keep copyright + licence
    opts.name_legacy = False
    opts.notdef_outline = True
    opts.drop_tables += ["DSIG"]
    sub = subset.Subsetter(options=opts)
    sub.populate(unicodes=list(CODEPOINTS))
    sub.subset(font)

    font.flavor = "woff2"
    # Otherwise head.modified moves every run and two identical builds differ.
    font.recalcTimestamp = False
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue(), copyright_line, missing


_HEADER = '''"""Embedded board-report faces — GENERATED, do not edit by hand.

Written by ``fonts/build_embedded.py`` from Google Fonts upstream, pinned at
commit ``{pin}``. Each family's SIL Open Font License text sits beside the
binaries in ``fonts/<Family>-OFL.txt``, and each family's copyright line is
carried into the rendered document as a CSS comment on its ``@font-face`` block.

Data, not behaviour: the renderer concatenates these strings, so the document is
byte-for-byte deterministic and nothing is generated at request time. Re-running
the build with the same inputs reproduces this file exactly.

Faces here back the weights the stylesheet actually renders. Weights that are
NOT here (Plex Mono 500 and 700, Fraunces italic) resolve through CSS weight
matching or synthetic oblique, and every glyph missing from a subset falls
through the family's fallback stack — which is why that stack stays.
"""
from __future__ import annotations

#: google/fonts commit these were subset from.
UPSTREAM_COMMIT = "{pin}"

#: Codepoints each face was subset to.
REPERTOIRE_SIZE = {repertoire}

#: ``(css family, css font-style, css font-weight, copyright, licence file,
#: base64 woff2)`` in the order the ``@font-face`` blocks are emitted.
FACES: tuple[tuple[str, str, str, str, str, str], ...] = (
'''


def emit(rows: list[tuple[str, str, str, str, str, str]]) -> str:
    out = [_HEADER.format(pin=UPSTREAM_COMMIT, repertoire=len(CODEPOINTS))]
    for family, style, weight, copyright_line, licence, b64 in rows:
        out.append("    (\n")
        for field in (family, style, weight, copyright_line, licence):
            out.append(f"        {field!r},\n")
        out.append("        (\n")
        for i in range(0, len(b64), 96):
            out.append(f"            {b64[i:i + 96]!r}\n")
        out.append("        ),\n    ),\n")
    out.append(")\n")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=pathlib.Path,
                    help="directory of upstream google/fonts files")
    ap.add_argument("--measure", action="store_true",
                    help="price every candidate, including the ones not shipped")
    args = ap.parse_args()

    if not args.src.is_dir():
        print(f"--src {args.src} is not a directory", file=sys.stderr)
        return 2

    for stem, family_dir in LICENCES:
        src = args.src / f"{family_dir}__OFL.txt"
        if not src.is_file():
            print(f"missing licence {src}", file=sys.stderr)
            return 2
        # Copied as BYTES. Reading as text would normalise the upstream line
        # endings, and a licence that no longer matches its source byte for byte
        # is a licence somebody has to argue about.
        raw = src.read_bytes()
        if "SIL OPEN FONT LICENSE Version 1.1" not in raw.decode("utf-8"):
            print(f"{src} is not an OFL 1.1 text", file=sys.stderr)
            return 2
        (HERE / f"{stem}-OFL.txt").write_bytes(raw)
        print(f"  licence  {stem}-OFL.txt  {len(raw):,} B")

    rows: list[tuple[str, str, str, str, str, str]] = []
    total_b64 = 0
    for family, style, filename, axes, weight, licence in FACES:
        prefix = {"Fraunces": "fraunces", "Inter": "inter",
                  "IBM Plex Mono": "ibmplexmono"}[family]
        data, copyright_line, missing = build_face(
            args.src / f"{prefix}__{filename}", axes)
        stem = f"{licence}-{weight.replace(' ', '-')}"
        if style != "normal":
            stem += f"-{style}"
        (HERE / f"{stem}.woff2").write_bytes(data)
        b64 = base64.b64encode(data).decode("ascii")
        total_b64 += len(b64)
        rows.append((family, style, weight, copyright_line, f"{licence}-OFL.txt", b64))
        print(f"  face     {stem}.woff2  woff2 {len(data):>7,} B  "
              f"base64 {len(b64):>7,} B  missing {len(missing)} cp"
              + (" (" + " ".join(f"U+{c:04X}" for c in missing) + ")" if missing else ""))

    # CRLF, like every other Python file in this tree.
    OUT_MODULE.write_text(emit(rows), encoding="utf-8", newline="\r\n")
    print(f"\n  module   {OUT_MODULE.name}  {OUT_MODULE.stat().st_size:,} B")
    print(f"  TOTAL base64 across {len(rows)} faces: {total_b64:,} B")

    if args.measure:
        print("\n  candidates (not written):")
        for label, filename, axes in MEASURE_ONLY:
            prefix = ("fraunces" if "Fraunces" in filename
                      else "inter" if "Inter" in filename else "ibmplexmono")
            data, _, _ = build_face(args.src / f"{prefix}__{filename}", axes)
            print(f"    {label:36} woff2 {len(data):>7,} B  "
                  f"base64 {len(base64.b64encode(data)):>7,} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
