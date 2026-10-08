"""The team chart: who reports to whom, and how a chart name finds a sign-in.

Seed data from the manager's spreadsheet. The managers' e-mails were given by
the owner on 2026-10-08 and are matched on e-mail only; every reportee still
has ``email=None`` and is matched to a ``users`` document by name, with the
rules below, until an admin fills the address in. Once an ``email`` is set the
name rules are not consulted for that person at all.

Everything here is plain data and pure functions. No Firestore, no clock —
``GET /api/usage/team`` (``app/routers/admin.py``) does the reads and hands
the user documents in.

Matching a chart person to a ``users`` document
----------------------------------------------
1. A chart entry with an ``email`` matches on lowercased e-mail only.
2. Otherwise both names are normalised: lowercased, anything in parentheses
   dropped (nicknames — "Mari Ann Belle S. Del Socorro (Mabs)"), punctuation
   stripped, split on whitespace, single-letter tokens dropped (middle
   initials). The chart's first token is ``first``, its last token ``last``.
3. A user matches when (a) ``first`` and ``last`` both appear in the user's
   normalised display-name tokens; or (b) the e-mail local part, split on
   ``.``/``_``/``-``, contains ``first`` and either ``last`` or a single letter
   equal to ``last[0]`` — ``chelsea.e@practice360.ai`` is Chelsea Estrella.
4. Exactly one user → ``"name"`` (or ``"email"`` by rule 1). More than one →
   ``"ambiguous"``: nobody is attached and the payload says so. None →
   ``"none"``, which means "has not signed in yet".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

MATCH_EMAIL = "email"
MATCH_NAME = "name"
MATCH_AMBIGUOUS = "ambiguous"
MATCH_NONE = "none"


@dataclass(frozen=True)
class Person:
    name: str
    title: str
    email: Optional[str] = None


@dataclass(frozen=True)
class Manager:
    person: Person
    reportees: tuple[Person, ...]


def _p(name: str, title: str, email: Optional[str] = None) -> Person:
    return Person(name=name, title=title, email=email)


#: The chart. Haylie Anne Logan (CMO) is deliberately absent: admins are
#: identified by ``is_admin`` on the caller, never by this table.
_ANGELICA = Manager(
    _p("Angelica Mhay Canlas-David", "Executive Assistant (Deliverable Management)",
       "angelica.david@legalsoft.com"),
    (
        _p("Francesca Canquin", "Client General Manager"),
        _p("Julian Rivera Gomez", "Client General Manager"),
        _p("Cleff Remegio", "Product Manager"),
        _p("Dexter Jumig", "Marketing Analyst"),
        _p("Nikki Riva Saludar", "Marketing Coordinator"),
        _p("Jennifer Melissa Orozco", "Marketing Coordinator"),
    ),
)
_KIER = Manager(
    _p("Kier Anthony M. Dela Rosa", "SEO Manager", "kier.delarosa@legalsoft.com"),
    (
        _p("Yans Suarez", "SEO Specialist"),
        _p("Marian Portillo", "SEO Specialist"),
        _p("Michael John Tayco", "SEO Specialist"),
        _p("Mahmoud Elsheikh", "SEO Specialist"),
        _p("Lynie Tinguban", "SEO Specialist"),
        _p("Chelsea Estrella", "SEO Specialist"),
    ),
)
_DANIEL = Manager(
    _p("Daniel Sernin Noche Amorsolo", "Graphic Designer", "daniel.amorsolo@legalsoft.com"),
    (_p("Brix Ayo", "Graphic Designer"),),
)
# The web team. Raj Dobariya led it on the sheet; on 2026-10-08 the owner set
# him aside as a manager, so the team reports to Anushka with him in it.
_WEB_TEAM: tuple[Person, ...] = (
    _p("Raj Dobariya", "Internal Web Developer"),
    _p("Robert Bob Mondigo", "Internal Web Developer"),
    _p("Sakir Showrov", "Internal Web Developer"),
    _p("Julius Cristobal", "UI/UX Designer"),
    _p("Kamran Shah", "UI/UX Designer"),
    _p("Mari Ann Belle S. Del Socorro (Mabs)", "UI/UX Designer"),
)
# Everyone on the chart reports to Anushka (owner, 2026-10-08): the three
# team managers, their reportees, and the web team.
_ANUSHKA = Manager(
    _p("Anushka Prasad", "Manager", "anushka.p@legalsoft.com"),
    tuple(
        [m.person for m in (_ANGELICA, _KIER, _DANIEL)]
        + [r for m in (_ANGELICA, _KIER, _DANIEL) for r in m.reportees]
        + list(_WEB_TEAM)
    ),
)

MANAGERS: tuple[Manager, ...] = (_ANGELICA, _KIER, _DANIEL, _ANUSHKA)


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

_PARENS = re.compile(r"\([^)]*\)")
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


def name_tokens(name: str) -> list[str]:
    """Lowercase, drop parentheticals and punctuation, drop one-letter tokens."""
    cleaned = _NOT_ALNUM.sub(" ", _PARENS.sub(" ", (name or "").lower()))
    return [t for t in cleaned.split() if len(t) > 1]


def local_part_tokens(email: str) -> list[str]:
    """``chelsea.e@x`` → ``["chelsea", "e"]``. Single letters are kept here —
    they are the initials rule 3(b) looks for."""
    local = (email or "").lower().split("@", 1)[0]
    return [t for t in re.split(r"[._-]+", local) if t]


def _first_last(person: Person) -> tuple[str, str] | None:
    tokens = name_tokens(person.name)
    if not tokens:
        return None
    return tokens[0], tokens[-1]


def user_matches(person: Person, user: dict[str, Any]) -> Optional[str]:
    """``"email"`` / ``"name"`` when ``user`` is this chart person, else None."""
    user_email = str(user.get("email") or "").lower()
    if person.email:
        return MATCH_EMAIL if user_email == person.email.lower() else None
    pair = _first_last(person)
    if pair is None:
        return None
    first, last = pair
    display = set(name_tokens(str(user.get("name") or "")))
    if first in display and last in display:
        return MATCH_NAME
    local = local_part_tokens(user_email)
    if first in local and (last in local or last[0] in local):
        return MATCH_NAME
    return None


def match_person(person: Person, users: Iterable[dict[str, Any]]) -> tuple[str, Optional[dict[str, Any]]]:
    """Resolve one chart person against the user directory.

    Returns ``(outcome, user)`` where ``outcome`` is one of the ``MATCH_*``
    constants and ``user`` is the single matched document, or None.
    """
    hits: list[tuple[str, dict[str, Any]]] = []
    for user in users:
        how = user_matches(person, user)
        if how:
            hits.append((how, user))
    if not hits:
        return MATCH_NONE, None
    if len(hits) > 1:
        return MATCH_AMBIGUOUS, None
    return hits[0]


def manager_for_user(user: dict[str, Any]) -> Optional[Manager]:
    """The chart manager this user document is, if it is exactly one of them."""
    hits = [m for m in MANAGERS if user_matches(m.person, user)]
    return hits[0] if len(hits) == 1 else None


def resolve_reportees(
    users: list[dict[str, Any]], manager: Optional[Manager] = None
) -> list[dict[str, Any]]:
    """Each reportee (of ``manager``, or of every manager) with its match.

    One dict per chart person: ``name, title, email, user_id, match, user`` —
    ``email``/``user_id``/``user`` are filled only on a single clean match,
    so an ambiguous person never has anybody's rows attributed to them.
    """
    managers = (manager,) if manager is not None else MANAGERS
    out: list[dict[str, Any]] = []
    for m in managers:
        for person in m.reportees:
            how, user = match_person(person, users)
            out.append(
                {
                    "name": person.name,
                    "title": person.title,
                    "email": (user.get("email") if user else person.email) or None,
                    "user_id": str(user["id"]) if user and user.get("id") else None,
                    "match": how,
                    "user": user,
                }
            )
    return out
