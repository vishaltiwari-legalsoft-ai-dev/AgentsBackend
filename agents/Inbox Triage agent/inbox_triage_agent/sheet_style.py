"""How the sheet looks: brand-blue headers, banded rows, readable widths, and
colour that says what each row is. Pure — it builds ``batchUpdate`` requests
from what a ``spreadsheets.get`` returned; ``sheet_writer`` does the I/O.

Applied ONCE, not on every hourly re-check: a spreadsheet-level developer
metadata entry (:data:`FORMAT_MARKER_KEY` = :data:`FORMAT_VERSION`) records
that the look was applied, and while it says so nothing here is sent again —
so a colour, width or rule she changes later stays hers. The marker is
written as the LAST request of the same atomic batch as the formatting, so it
exists exactly when the formatting does. Bumping :data:`FORMAT_VERSION`
re-applies the look once on every sheet.

Re-applying never duplicates and never touches hers:

* every conditional rule the agent writes is a custom formula carrying
  :data:`RULE_TAG` (``N("agentos-a12")=0`` is always true, so it changes no
  result). Sheets rewrites a rule's cell references when columns move, but
  not a string literal, so the tag identifies the agent's rules even after
  she inserts columns. Only tagged rules are deleted before re-adding; hers
  are left in place, and keep precedence over the agent's.
* the agent's banding has a fixed id (:data:`INBOX_BANDING_ID`); only that
  id is ever deleted. If she banded the Inbox tab herself, the agent adds
  none — Sheets refuses overlapping banding, and hers wins.
"""

from __future__ import annotations

from .sheet_layout import (
    CATEGORY_LABELS, COL_CATEGORY, COL_DEADLINE, COL_STATUS, DUE_OVERDUE, DUE_UPCOMING,
    HEADERS, STATUS_DONE, STATUS_IN_PROGRESS, UPCOMING_HEADERS, column_letter,
)
from .triage import NEEDS_REVIEW

FORMAT_MARKER_KEY = "agentos.a12.format"
FORMAT_VERSION = "1"
#: Always-true term in every agent rule's formula — see the module docstring.
RULE_TAG = 'N("agentos-a12")=0'
_TAG_TEXT = "agentos-a12"
#: The agent's banding on Inbox. Ids are per spreadsheet; this one is the
#: agent's by convention and is the only banding it ever deletes.
INBOX_BANDING_ID = 712012

#: Fields the formatting pass reads (one ``spreadsheets.get``).
FORMAT_READ_FIELDS = (
    "developerMetadata(metadataId,metadataKey,metadataValue),"
    "sheets(properties(sheetId,title),conditionalFormats,bandedRanges(bandedRangeId))"
)

# --------------------------------------------------------------------------- #
# Palette — soft fills under dark text; the only light-on-dark pair is white
# on the header blue and on the Overdue red. Contrast is pinned in the tests.
# --------------------------------------------------------------------------- #
BRAND_BLUE = "#1746A2"
WHITE = "#FFFFFF"
BAND_TINT = "#F3F6FC"

#: ``category label → (fill, text)``; a fill of ``None`` colours the text only.
CATEGORY_STYLE: dict[str, tuple[str | None, str]] = {
    CATEGORY_LABELS["action_required"]: ("#F9D6D5", "#7A1A16"),
    CATEGORY_LABELS["reply_needed"]: ("#FDE3C7", "#7A3B00"),
    CATEGORY_LABELS["meeting"]: ("#D7E5FB", "#0E3677"),
    CATEGORY_LABELS["finance"]: ("#D4F0DC", "#18562E"),
    CATEGORY_LABELS["notification"]: ("#E7E1F4", "#46366B"),
    CATEGORY_LABELS["fyi"]: ("#ECEDEF", "#33373D"),
    CATEGORY_LABELS["newsletter_promo"]: (None, "#8A8F98"),
    CATEGORY_LABELS[NEEDS_REVIEW]: ("#FFE69A", "#5E4500"),
}
DONE_FILL, DONE_TEXT = "#E2F2E5", "#7D8580"
IN_PROGRESS_FILL, IN_PROGRESS_TEXT = "#FFF3BF", "#574400"
DEADLINE_SOON_TEXT = "#B8420A"
DEADLINE_OVERDUE_TEXT = "#A3161A"
DUE_OVERDUE_FILL, DUE_OVERDUE_TEXT = "#C0282D", WHITE
DUE_SOON_FILL, DUE_SOON_TEXT = "#FFD6A5", "#6E3500"
DUE_LATER_FILL, DUE_LATER_TEXT = "#D4F0DC", "#18562E"

HEADER_ROW_PIXELS = 34
#: Width in pixels by column name, both tabs. Message ID is hidden: no width.
COLUMN_WIDTHS: dict[str, int] = {
    "Date": 130, "Received": 130, "From": 200, "Subject": 260, "Category": 140,
    "Summary": 380, "Action": 300, "Deadline": 110, "Link": 90, "Status": 110,
    "Notes": 220, "Due": 100,
}
WRAPPED = ("Summary", "Action")
CLIPPED = ("Link",)


def rgb(hex_colour: str) -> dict:
    h = hex_colour.lstrip("#")
    return {k: int(h[i:i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}


def _cell_format(fill: str | None, text: str | None, *, bold: bool = False,
                 strike: bool = False) -> dict:
    fmt: dict = {}
    if fill:
        fmt["backgroundColor"] = rgb(fill)
    text_format: dict = {}
    if text:
        text_format["foregroundColor"] = rgb(text)
    if bold:
        text_format["bold"] = True
    if strike:
        text_format["strikethrough"] = True
    if text_format:
        fmt["textFormat"] = text_format
    return fmt


def tagged(condition: str) -> str:
    """A custom-formula rule body, tagged as the agent's."""
    return f"=AND({condition}, {RULE_TAG})"


def is_agent_rule(rule: dict) -> bool:
    condition = ((rule or {}).get("booleanRule") or {}).get("condition") or {}
    return any(
        _TAG_TEXT in str((v or {}).get("userEnteredValue") or "").lower()
        for v in condition.get("values") or []
    )


def marker_value(meta: dict) -> str | None:
    """The formatting marker's value in a ``spreadsheets.get`` result, or
    ``None`` when the sheet has never been formatted by the agent."""
    for entry in (meta or {}).get("developerMetadata") or []:
        if entry.get("metadataKey") == FORMAT_MARKER_KEY:
            return str(entry.get("metadataValue") or "")
    return None


def is_formatted(meta: dict) -> bool:
    return marker_value(meta) == FORMAT_VERSION


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #

def _rows_from_2(sheet_id: int, col: int | None = None, *, width: int | None = None) -> dict:
    """Whole column(s) from row 2 down, open-ended so future rows are covered.
    ``col`` is 1-based (one column); ``width`` covers columns 1..width."""
    rng = {"sheetId": sheet_id, "startRowIndex": 1}
    if col is not None:
        rng.update(startColumnIndex=col - 1, endColumnIndex=col)
    else:
        rng.update(startColumnIndex=0, endColumnIndex=width)
    return rng


def _rule(rng: dict, condition: str, fmt: dict, index: int) -> dict:
    return {
        "addConditionalFormatRule": {
            "index": index,
            "rule": {
                "ranges": [rng],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                  "values": [{"userEnteredValue": tagged(condition)}]},
                    "format": fmt,
                },
            },
        }
    }


def inbox_rules(sheet_id: int) -> list[tuple[dict, str, dict]]:
    """``(range, condition, format)`` in precedence order: a Done row first
    (it wins over every other colour in that row), then Status, Deadline,
    and Category. Column letters come from the ``COL_*`` constants."""
    status = f"${column_letter(COL_STATUS)}2"
    deadline = f"${column_letter(COL_DEADLINE)}2"
    category = f"${column_letter(COL_CATEGORY)}2"
    today = 'TEXT(TODAY(), "yyyy-mm-dd")'
    in_two_days = 'TEXT(TODAY()+2, "yyyy-mm-dd")'
    open_deadline = f'{deadline}<>"", {status}<>"{STATUS_DONE}"'
    rules: list[tuple[dict, str, dict]] = [
        (_rows_from_2(sheet_id, width=len(HEADERS)), f'{status}="{STATUS_DONE}"',
         _cell_format(DONE_FILL, DONE_TEXT, strike=True)),
        (_rows_from_2(sheet_id, COL_STATUS), f'{status}="{STATUS_IN_PROGRESS}"',
         _cell_format(IN_PROGRESS_FILL, IN_PROGRESS_TEXT, bold=True)),
        # Deadline cells are ISO text, so text comparison is date comparison.
        (_rows_from_2(sheet_id, COL_DEADLINE), f"{open_deadline}, {deadline}<{today}",
         _cell_format(None, DEADLINE_OVERDUE_TEXT, bold=True)),
        (_rows_from_2(sheet_id, COL_DEADLINE),
         f"{open_deadline}, {deadline}>={today}, {deadline}<={in_two_days}",
         _cell_format(None, DEADLINE_SOON_TEXT, bold=True)),
    ]
    for label, (fill, text) in CATEGORY_STYLE.items():
        rules.append((_rows_from_2(sheet_id, COL_CATEGORY), f'{category}="{label}"',
                      _cell_format(fill, text, bold=label != CATEGORY_LABELS["newsletter_promo"])))
    return rules


def upcoming_rules(sheet_id: int) -> list[tuple[dict, str, dict]]:
    due_col = UPCOMING_HEADERS.index("Due") + 1
    due = f"${column_letter(due_col)}2"
    deadline = f"${column_letter(UPCOMING_HEADERS.index('Deadline') + 1)}2"
    tomorrow = 'TEXT(TODAY()+1, "yyyy-mm-dd")'
    cell = _rows_from_2(sheet_id, due_col)
    return [
        (cell, f'{due}="{DUE_OVERDUE}"', _cell_format(DUE_OVERDUE_FILL, DUE_OVERDUE_TEXT, bold=True)),
        (dict(cell), f'{due}="{DUE_UPCOMING}", {deadline}<={tomorrow}',
         _cell_format(DUE_SOON_FILL, DUE_SOON_TEXT, bold=True)),
        (dict(cell), f'{due}="{DUE_UPCOMING}"', _cell_format(DUE_LATER_FILL, DUE_LATER_TEXT)),
    ]


def _header_requests(sheet_id: int, width: int) -> list[dict]:
    return [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": width},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": rgb(BRAND_BLUE),
                    "textFormat": {"foregroundColor": rgb(WHITE), "bold": True},
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "CLIP",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": HEADER_ROW_PIXELS},
                "fields": "pixelSize",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
    ]


def _body_requests(sheet_id: int, headers: tuple[str, ...]) -> list[dict]:
    """Widths, top alignment, wrap/clip — by column NAME, so a header list
    change moves them with it. Only width and alignment fields are written;
    her fonts and colours in the body are left alone."""
    requests: list[dict] = []
    for index, name in enumerate(headers):
        width = COLUMN_WIDTHS.get(name)
        if width:  # Message ID has none: it stays hidden
            requests.append({
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                              "startIndex": index, "endIndex": index + 1},
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            })
    requests.append({
        "repeatCell": {
            "range": _rows_from_2(sheet_id, width=len(headers)),
            "cell": {"userEnteredFormat": {"verticalAlignment": "TOP"}},
            "fields": "userEnteredFormat.verticalAlignment",
        }
    })
    for names, strategy in ((WRAPPED, "WRAP"), (CLIPPED, "CLIP")):
        for name in names:
            if name in headers:
                requests.append({
                    "repeatCell": {
                        "range": _rows_from_2(sheet_id, headers.index(name) + 1),
                        "cell": {"userEnteredFormat": {"wrapStrategy": strategy}},
                        "fields": "userEnteredFormat.wrapStrategy",
                    }
                })
    return requests


def _sheet(meta: dict, sheet_id: int) -> dict:
    for sheet in (meta or {}).get("sheets") or []:
        if int(((sheet.get("properties") or {}).get("sheetId")) or 0) == sheet_id:
            return sheet
    return {}


def format_requests(meta: dict, *, inbox_sheet_id: int, upcoming_sheet_id: int) -> list[dict]:
    """The whole look as one ordered request list, built against ``meta``
    (a :data:`FORMAT_READ_FIELDS` read): the agent's own previous rules,
    banding and marker are removed first, then everything is added, and the
    marker is last."""
    requests: list[dict] = []
    her_rule_counts: dict[int, int] = {}
    for sheet_id in (inbox_sheet_id, upcoming_sheet_id):
        rules = _sheet(meta, sheet_id).get("conditionalFormats") or []
        ours = [i for i, rule in enumerate(rules) if is_agent_rule(rule)]
        her_rule_counts[sheet_id] = len(rules) - len(ours)
        # Highest index first, so each deletion leaves the next index valid.
        requests += [
            {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}}
            for i in sorted(ours, reverse=True)
        ]

    inbox_bands = [
        b.get("bandedRangeId") for b in _sheet(meta, inbox_sheet_id).get("bandedRanges") or []
    ]
    if INBOX_BANDING_ID in inbox_bands:
        requests.append({"deleteBanding": {"bandedRangeId": INBOX_BANDING_ID}})
    her_banding = any(b != INBOX_BANDING_ID for b in inbox_bands)

    for entry in (meta or {}).get("developerMetadata") or []:
        if entry.get("metadataKey") == FORMAT_MARKER_KEY and entry.get("metadataId") is not None:
            requests.append({"deleteDeveloperMetadata": {"dataFilter": {
                "developerMetadataLookup": {"metadataId": entry["metadataId"]}}}})

    requests += _header_requests(inbox_sheet_id, len(HEADERS))
    requests += _header_requests(upcoming_sheet_id, len(UPCOMING_HEADERS))
    requests += _body_requests(inbox_sheet_id, HEADERS)
    requests += _body_requests(upcoming_sheet_id, UPCOMING_HEADERS)
    if not her_banding:
        requests.append({
            "addBanding": {"bandedRange": {
                "bandedRangeId": INBOX_BANDING_ID,
                "range": _rows_from_2(inbox_sheet_id, width=len(HEADERS)),
                "rowProperties": {"firstBandColor": rgb(WHITE), "secondBandColor": rgb(BAND_TINT)},
            }}
        })
    # After hers: her own rules keep precedence over the agent's.
    for sheet_id, rules in ((inbox_sheet_id, inbox_rules(inbox_sheet_id)),
                            (upcoming_sheet_id, upcoming_rules(upcoming_sheet_id))):
        start = her_rule_counts[sheet_id]
        requests += [_rule(rng, cond, fmt, start + i) for i, (rng, cond, fmt) in enumerate(rules)]

    requests.append({
        "createDeveloperMetadata": {"developerMetadata": {
            "metadataKey": FORMAT_MARKER_KEY,
            "metadataValue": FORMAT_VERSION,
            "location": {"spreadsheet": True},
            "visibility": "DOCUMENT",
        }}
    })
    return requests
