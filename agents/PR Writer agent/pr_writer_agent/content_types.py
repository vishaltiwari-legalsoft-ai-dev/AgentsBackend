"""The eight PR content types as data (blueprint §3 + §7 release anatomy).

Prompts, the renderer and QA all read these records, so a rule changes in one
place. ``parts`` are the block kinds a draft of that type must contain, in
reading order; the renderer adds what code owns (release line, dateline,
boilerplate, media contact, ``###``) and the model never writes those.

Word ranges count the body only: lede through call to action, quotes
included; headline, subhead, dateline, boilerplate, bio and media contact
excluded.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Every block kind a draft may contain. The schema in prompts.py is built
# from this tuple so a kind cannot exist in one place and not the other.
BLOCK_KINDS: tuple[str, ...] = (
    # release anatomy
    "lede", "context", "quote", "detail", "cta",
    # product launch
    "problem", "solution", "capabilities", "availability", "proof",
    # partnership
    "partnership", "customer_benefit",
    # milestone
    "milestone", "next",
    # award
    "award", "criteria", "significance",
    # event
    "event_details", "speakers", "registration",
    # media advisory (who / what / when / where / why)
    "who", "what", "when", "where", "why",
    # thought leadership / byline
    "thesis", "evidence", "insight", "forward_look", "bio",
)

RELEASE_LINES = ("FOR IMMEDIATE RELEASE", "EMBARGOED UNTIL", "MEDIA ADVISORY")


@dataclass(frozen=True)
class ContentType:
    id: str
    label: str
    purpose: str
    words: tuple[int, int]
    parts: tuple[str, ...]                 # required block kinds, reading order
    optional_parts: tuple[str, ...] = ()
    quotes: tuple[int, int] = (1, 2)       # (min, max) proposed quotes
    boilerplates: int = 1                  # 0, 1 (brand) or 2 (brand + partner)
    release_line: bool = True
    dateline: bool = True
    headline_chars: tuple[int, int] | None = (70, 100)
    subhead_chars: tuple[int, int] | None = (120, 160)
    lede_words: tuple[int, int] | None = (25, 35)
    media_contact: bool = True
    url_parts: tuple[str, ...] = ("cta",)  # block kinds that must carry a URL
    first_person: bool = False
    company_mentions_max: int | None = None  # byline: once, and only in the bio
    outlets: tuple[str, ...] = ()
    structure_note: str = ""
    extra_rules: tuple[str, ...] = field(default_factory=tuple)

    def allowed_kinds(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.parts + self.optional_parts))


CONTENT_TYPES: dict[str, ContentType] = {
    ct.id: ct
    for ct in (
        ContentType(
            id="press_release",
            label="Press release",
            purpose="Announce hard news formally",
            words=(400, 600),
            parts=("lede", "context", "quote", "detail", "cta"),
            optional_parts=("proof",),
            quotes=(1, 2),
            outlets=("Newswire", "company newsroom", "trade press"),
            structure_note=(
                "Lede (who/what/when/where/why), context paragraph, executive quote "
                "(perspective, not facts), supporting detail (features, data, "
                "availability, pricing, partner role), optional second quote "
                "(partner, customer or analyst), call to action with URL."
            ),
        ),
        ContentType(
            id="media_advisory",
            label="Media announcement / advisory",
            purpose="Alert media to an upcoming event or availability",
            words=(150, 300),
            parts=("who", "what", "when", "where", "why"),
            optional_parts=("context",),
            quotes=(0, 0),
            boilerplates=0,
            subhead_chars=None,
            lede_words=None,
            url_parts=(),
            outlets=("Journalists", "assignment desks", "event calendars"),
            structure_note=(
                "Who, what, when (date, time and time zone), where, why — short, "
                "scannable lines for an assignment desk. No quotes."
            ),
            extra_rules=("when_has_timezone",),
        ),
        ContentType(
            id="product_launch",
            label="Product launch",
            purpose="Introduce a product or major feature",
            words=(500, 700),
            parts=("lede", "problem", "solution", "capabilities", "availability",
                   "proof", "quote", "cta"),
            quotes=(1, 2),
            outlets=("Newswire", "trade and tech press", "product page"),
            structure_note=(
                "Lede, the problem, the solution, key capabilities (what it does, "
                "its limits, human review where AI is involved), availability and "
                "pricing (only if the brief allows pricing), customer or analyst "
                "proof (a [VERIFY] placeholder if none is in the evidence), quote, "
                "call to action."
            ),
        ),
        ContentType(
            id="partnership",
            label="Partnership",
            purpose="Announce an alliance, integration or reseller deal",
            words=(400, 600),
            parts=("lede", "partnership", "customer_benefit", "quote", "cta"),
            optional_parts=("context", "detail"),
            quotes=(2, 3),
            boilerplates=2,
            outlets=("Newswire", "both companies' newsrooms"),
            structure_note=(
                "Lede, what the partnership does, benefit to customers, one quote "
                "from each party (both pending approval; the partner's needs that "
                "organization's written approval), call to action, both "
                "boilerplates."
            ),
            extra_rules=("quotes_from_both_parties",),
        ),
        ContentType(
            id="milestone",
            label="Company milestone",
            purpose="Mark growth, funding, customer count, anniversary or expansion",
            words=(350, 550),
            parts=("lede", "milestone", "context", "next", "quote"),
            optional_parts=("cta",),
            quotes=(1, 2),
            url_parts=(),
            outlets=("Newsroom", "business and trade press"),
            structure_note=(
                "Lede, the milestone fact, context, what it enables next, quote. "
                "No revenue, funding or valuation figures unless the evidence "
                "marks them approved."
            ),
        ),
        ContentType(
            id="award",
            label="Award / recognition",
            purpose="Announce an award, ranking or certification",
            words=(300, 450),
            parts=("lede", "award", "criteria", "significance", "quote"),
            optional_parts=("cta",),
            quotes=(1, 2),
            url_parts=(),
            outlets=("Newsroom", "LinkedIn", "trade press"),
            structure_note=(
                "Lede, the award and the awarding body (exact names), the criteria, "
                "why it matters, quote. Award marks only with documented permission."
            ),
        ),
        ContentType(
            id="event",
            label="Event announcement",
            purpose="Promote a webinar, conference presence, speaking slot or hosted event",
            words=(250, 450),
            parts=("lede", "event_details", "speakers", "registration", "quote"),
            quotes=(1, 2),
            url_parts=("registration",),
            outlets=("Newsroom", "event listings", "social"),
            structure_note=(
                "Lede, the event with date, time and time zone, speakers (exact "
                "names and titles), registration link, quote."
            ),
            extra_rules=("event_has_timezone",),
        ),
        ContentType(
            id="thought_leadership",
            label="Thought leadership / byline",
            purpose="Position an executive as an expert on an industry issue",
            words=(700, 1200),
            parts=("thesis", "evidence", "insight", "forward_look", "bio"),
            quotes=(0, 0),
            boilerplates=0,
            release_line=False,
            dateline=False,
            headline_chars=None,
            subhead_chars=None,
            lede_words=None,
            media_contact=False,
            url_parts=(),
            first_person=True,
            company_mentions_max=1,
            outlets=("Trade publications", "LinkedIn articles", "company blog"),
            structure_note=(
                "Written in the executive's own first-person voice: a strong thesis, "
                "evidence, practical insight, a forward look, and a short author "
                "bio. Minimal self-promotion — the company is named at most once, "
                "in the bio, and nowhere in the body. Follow the target "
                "publication's contributor guidelines when known."
            ),
        ),
    )
}

# Words excluded from the body count (render-owned or non-body kinds).
NON_BODY_KINDS = frozenset({"bio"})


def get(content_type_id: str) -> ContentType:
    try:
        return CONTENT_TYPES[content_type_id]
    except KeyError:
        raise ValueError(
            f"unknown content type {content_type_id!r}; expected one of "
            + ", ".join(CONTENT_TYPES)
        ) from None


def spec_for_prompt(ct: ContentType) -> str:
    """The content-type rules as the draft prompt states them."""
    lines = [
        f"Content type: {ct.label} — {ct.purpose}.",
        f"Body length: {ct.words[0]}–{ct.words[1]} words (lede through call to action; "
        "excludes headline, subhead, boilerplate, bio and media contact).",
        f"Structure: {ct.structure_note}",
        "Required block kinds, in order: " + ", ".join(ct.parts) + ".",
    ]
    if ct.optional_parts:
        lines.append("Optional block kinds: " + ", ".join(ct.optional_parts) + ".")
    lines.append("Use no other block kinds: anything else (e.g. a contact or sign-up line) goes "
                 "inside the closest listed block.")
    if ct.quotes[1] == 0:
        lines.append("No quote blocks.")
    else:
        lines.append(f"Quote blocks: {ct.quotes[0]}–{ct.quotes[1]}, each with a speaker_id.")
    if ct.headline_chars:
        lines.append(
            f"Headline: active voice, present tense, {ct.headline_chars[0]}–"
            f"{ct.headline_chars[1]} characters, brand name early."
        )
    if ct.subhead_chars:
        lines.append(f"Subheadline: one sentence, {ct.subhead_chars[0]}–{ct.subhead_chars[1]} characters.")
    else:
        lines.append("No subheadline: return an empty subhead_options list.")
    if ct.lede_words:
        lines.append(
            f"Lede: {ct.lede_words[0]}–{ct.lede_words[1]} words, one sentence, who/what/"
            "when/where/why. Do not write the dateline; code adds it."
        )
    if ct.release_line:
        lines.append(
            "release_line: \"FOR IMMEDIATE RELEASE\", or \"EMBARGOED UNTIL <time> "
            "<zone>, <AP date>\" when the brief states an embargo"
            + (", or \"MEDIA ADVISORY\"." if ct.id == "media_advisory" else ".")
        )
    else:
        lines.append("release_line: empty string.")
    if ct.url_parts:
        lines.append("These blocks must contain a URL from the ALLOWED URLS list: " + ", ".join(ct.url_parts) + ".")
    if ct.first_person:
        lines.append(
            "Write in the first person (I, we) as the executive. Name the company "
            "only in the bio block, once."
        )
    return "\n".join(lines)
