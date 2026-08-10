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
# also never attaches the debugger on the extension side.
MUTATING_KINDS = frozenset({"navigate", "click", "type", "select", "key", "open_tab"})

# Handled entirely on the server: the model uses this to say a sub-task of the
# plan is finished, and we immediately ask it for a real action on the next one.
# Deliberately NOT in ACTION_KINDS — the extension's wire contract is unchanged,
# so adding planning did not need a protocol bump.
INTERNAL_KINDS = ("next_subtask",)


class Action(BaseModel):
    kind: str
    index: int | None = None  # distilled-DOM element index (click/type/select)
    url: str | None = None  # navigate / open_tab
    text: str | None = None  # type: text · key: key combo · ask_user: question
    value: str | None = None  # select: option value or visible label
    tab_id: int | None = None  # switch_tab
    direction: str = "down"  # scroll: up | down
    why: str = ""  # one-line rationale, shown verbatim in the step log
    summary: str | None = None  # done: final answer for the user
    extracted: list | dict | None = None  # done: structured findings
    reason: str | None = None  # fail: honest reason


_REQUIRED: dict[str, tuple[str, ...]] = {
    "navigate": ("url",),
    "click": ("index",),
    "type": ("index", "text"),
    "select": ("index", "value"),
    "key": ("text",),
    "open_tab": ("url",),
    "switch_tab": ("tab_id",),
    "ask_user": ("text",),
    "done": ("summary",),
    "fail": ("reason",),
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


def is_sensitive(action: Action, elements: list[dict]) -> bool:
    if action.kind in ("navigate", "open_tab"):
        url = (action.url or "").lower()
        return any(tok in url for tok in _SENSITIVE_URL_TOKENS)
    presses_enter = action.kind == "key" and "enter" in (action.text or "").lower()
    if action.kind in ("click", "select") or presses_enter:
        el = next((e for e in elements if e.get("i") == action.index), None)
        if not el:
            return False
        label = " ".join(str(el.get(k) or "") for k in ("text", "name", "role")).lower()
        return _label_is_sensitive(label)
    return False
