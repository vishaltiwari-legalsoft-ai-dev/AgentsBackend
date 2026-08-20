"""Action grammar + safety policy for the Browser Agent (a11).

The action set is a FIXED enum — that is the safety boundary between the LLM
brain and the user's real, logged-in browser. The Chrome extension mirrors this
schema in ``src/protocol.ts`` (browser-extension repo); both sides pin the same
JSON fixtures in their test suites. Bump ``PROTOCOL`` on any breaking change so
an outdated extension gets an honest 409 instead of a silent misparse.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel

PROTOCOL = 1

# Everything the brain may ask the extension to do. An unknown kind from the
# model is rejected and surfaces as an honest failure — never guessed.
ACTION_KINDS = (
    "navigate",
    "click",
    "type",
    "select",
    "key",
    "scroll",
    "switch_tab",
    "open_tab",
    "wait",
    "extract",
    "ask_user",
    "done",
    "fail",
)

# Kinds that drive the page — forbidden in monitor (read-only) mode, which
# also never attaches the debugger on the extension side. "tool" is here even
# though it never reaches the page: the toolbox can write (sheet_append), and a
# read-only run that writes to a spreadsheet is still a read-only run breached.
MUTATING_KINDS = frozenset(
    {"navigate", "click", "type", "select", "key", "open_tab", "tool"}
)

# Handled entirely on the server, never sent to the extension. "next_subtask"
# marks a plan milestone done; "tool" reaches an API instead of the browser.
# Deliberately NOT in ACTION_KINDS — the extension's wire contract is unchanged,
# so neither planning nor the toolbox needed a protocol bump.
INTERNAL_KINDS = ("next_subtask", "tool")


class Action(BaseModel):
    kind: str
    index: int | None = None  # distilled-DOM element index (click/type/select)
    # The element's label, copied from the list. Indexes are re-assigned every
    # step, so a dropdown opening renumbers the page underneath the model — the
    # extension therefore finds the element BY THIS NAME and treats the index as
    # a tie-break only. Without it, a click lands on whatever inherited the
    # number, which is how "cancel" once resolved to "send".
    expect: str | None = None
    role: str | None = None  # narrows the search when a label is ambiguous
    # What should be observably true after this works. Checked on the next step
    # against what actually happened — verification that costs no extra LLM call.
    expect_after: str | None = None
    url: str | None = None  # navigate / open_tab
    text: str | None = None  # type: text · key: key combo · ask_user: question
    value: str | None = None  # select: option value or visible label
    tab_id: int | None = None  # switch_tab
    direction: str = "down"  # scroll: up | down
    why: str = ""  # one-line rationale, shown verbatim in the step log
    summary: str | None = None  # done: final answer for the user
    extracted: list | dict | None = None  # done: structured findings
    reason: str | None = None  # fail: honest reason
    # tool: which API to reach for, and its arguments. Server-side only.
    tool: str | None = None
    sheet: str | None = None
    tab: str | None = None
    rows: list | None = None
    query: str | None = None


# Actions that must say WHICH element they mean. Either identifier will do:
# the name is what actually resolves it, and a replayed or recorded step
# carries only a name — a saved index would be meaningless on the next visit.
_NEEDS_TARGET = frozenset({"click", "type", "select"})

_REQUIRED: dict[str, tuple[str, ...]] = {
    "navigate": ("url",),
    "type": ("text",),
    "select": ("value",),
    "key": ("text",),
    "open_tab": ("url",),
    "switch_tab": ("tab_id",),
    "ask_user": ("text",),
    "done": ("summary",),
    "fail": ("reason",),
    "tool": ("tool",),
}


def validate_action(raw: object) -> Action:
    """Strict-parse a raw model action; raises ValueError with the honest gap."""
    if not isinstance(raw, dict):
        raise ValueError("action must be a JSON object")
    kind = raw.get("kind")
    if kind not in ACTION_KINDS and kind not in INTERNAL_KINDS:
        raise ValueError(f"model proposed unknown action {kind!r}")
    known = {k: v for k, v in raw.items() if k in Action.model_fields}
    action = Action(**known)  # pydantic ValidationError is a ValueError
    for field in _REQUIRED.get(str(kind), ()):
        if getattr(action, field) in (None, ""):
            raise ValueError(f'action "{kind}" is missing required field "{field}"')
    if kind in _NEEDS_TARGET and action.index is None and not action.expect:
        raise ValueError(
            f'action "{kind}" must say which element it means — give "expect" '
            "(the element's name) and optionally its index"
        )
    return action


# --------------------------------------------------------------------------- #
# Domain policy
# --------------------------------------------------------------------------- #

# Financial / credential surfaces the agent must never drive. Override with
# BROWSER_BLOCKED_DOMAINS; BROWSER_ALLOWED_DOMAINS (empty = everything else).
DEFAULT_BLOCKED_DOMAINS = (
    "paypal.com,stripe.com,pay.google.com,accounts.google.com,"
    "chase.com,bankofamerica.com,wellsfargo.com,citi.com,"
    "hdfcbank.com,icicibank.com,onlinesbi.sbi,"
    "coinbase.com,binance.com"
)


def parse_domains(csv: str) -> set[str]:
    return {d.strip().lower().lstrip(".") for d in (csv or "").split(",") if d.strip()}


def domain_of(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _matches(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def check_url(url: str, allowed: set[str], blocked: set[str]) -> str | None:
    """None when navigation is allowed, else an honest refusal message."""
    host = domain_of(url)
    scheme = urlparse(url or "").scheme
    if not host or scheme not in ("http", "https"):
        return f"refusing to open non-web URL {url!r}"
    if _matches(host, blocked):
        return f"{host} is on the blocked-domain list (BROWSER_BLOCKED_DOMAINS)"
    if allowed and not _matches(host, allowed):
        return f"{host} is not on the allow-list (BROWSER_ALLOWED_DOMAINS)"
    return None


# --------------------------------------------------------------------------- #
# Sensitive-action classifier
# --------------------------------------------------------------------------- #

# Verbs that commit real-world consequences. A click/select/Enter on an element
# whose label matches — or navigation to a checkout-ish URL — requires an
# explicit user confirmation in the side panel before the extension executes.
SENSITIVE_VERBS = (
    "pay",
    "buy",
    "purchase",
    "checkout",
    "place order",
    "confirm order",
    "delete",
    "remove",
    "send",
    "transfer",
    "subscribe",
)
_SENSITIVE_URL_TOKENS = ("checkout", "payment", "billing")


def _label_is_sensitive(label: str) -> bool:
    for verb in SENSITIVE_VERBS:
        if " " in verb:
            if verb in label:
                return True
        elif re.search(rf"\b{re.escape(verb)}\b", label):
            return True
    return False


#: Tools that change something on the other side of the API. Everything else in
#: the toolbox only reads, and gating reads would bury an ordinary run in
#: confirmation prompts.
WRITING_TOOLS = frozenset({"sheet_append"})

#: Reading a workbook is how a run earns the right to append to it — see
#: ``check_sheet_target``. These are the tools that count as having read one.
SHEET_READING_TOOLS = frozenset({"sheet_read", "sheet_tabs"})


def check_sheet_target(target_id: str, permitted: set[str]) -> str | None:
    """None when this workbook may be appended to, else an honest refusal.

    The DESTINATION of a write is the one argument an injected page most wants
    control of: the append itself is safely built (own tab, RAW values, append
    only), so the way to turn it into an exfiltration channel is to change
    where it points. It is therefore not taken on trust — the caller supplies
    the workbooks a person actually chose, and nothing else is writable.
    """
    if not target_id:
        return "that doesn't look like a Google Sheet link, so I won't write to it"
    if target_id in permitted:
        return None
    return (
        "I only append to a workbook you named or one I have already read in "
        f"this run — {target_id} is neither, so I am not writing to it"
    )


def is_sensitive(action: Action, elements: list[dict]) -> bool:
    # Tool calls reach APIs, not the page — but a write is a write. The append
    # lands in a spreadsheet the model named, so a human belongs in front of it;
    # the read-only tools stay quiet.
    if action.kind == "tool":
        return str(action.tool or "") in WRITING_TOOLS
    if action.kind in ("navigate", "open_tab"):
        url = (action.url or "").lower()
        return any(tok in url for tok in _SENSITIVE_URL_TOKENS)
    presses_enter = action.kind == "key" and "enter" in (action.text or "").lower()
    if action.kind in ("click", "select") or presses_enter:
        # The label may come from the action itself (a replayed skill step
        # carries a name and no index) or from the element the index points at.
        # Checking only the index used to mean an index-less "Send" click
        # skipped confirmation entirely.
        labels = [str(action.expect or "")]
        el = next((e for e in elements if e.get("i") == action.index), None)
        if el:
            labels += [str(el.get(k) or "") for k in ("text", "name", "role")]
        return _label_is_sensitive(" ".join(labels).lower())
    return False
