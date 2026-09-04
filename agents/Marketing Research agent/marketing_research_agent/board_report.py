"""Board report — the metric catalog and the period roll-up behind it.

Two things live here and nothing else (no routes, no HTML, no PDF):

* :data:`CATALOG` — the report's metrics **as data**. One entry per published
  row carrying its label, group, number format, polarity, and either the sheet
  field it reads or the formula that recomputes it. The renderer reads this and
  never a sheet label, so a row the sheet renames is a parser change, not a
  renderer change.
* :func:`roll_up` / :func:`compare` — many months of the roll-up tab's official
  figures collapsed into one period, and two such periods laid side by side in
  the ledger shape the report renders. The roll-up also carries the period's
  months through *uncollapsed* — but only :data:`MONTHLY_FIELDS`, the three
  series the by-month chart draws, because a cell per month per metric would
  put 38 x N numbers into every stored run to draw three bars.

Three rules are load-bearing enough to say up front, because each one has a
wrong-number failure mode that looks entirely plausible on a board slide:

1. **CAC is a name collision.** ``CampaignMetric.cac`` (schemas.py) is
   ``spend / demos_completed`` — its own comment calls it a closed/won *proxy* —
   and on Q1 it is $880.81. The report's CAC is ``spend / revenue clients``,
   $4,991.28 on the same quarter. Same word, 5.7x apart. This module never uses
   the bare name: the published metric is :data:`CAC_KEY`
   (``cac_per_revenue_client``), it is *derived* rather than read, and the
   sheet's own ``cac`` row is on the denylist below so nothing can wire the two
   together.
2. **A ratio is recomputed from summed components, never averaged.** Averaging
   the monthly ROAS figures instead of recomputing from summed revenue and spend
   measures +1.74pp of error on this data. Every ratio in the catalog therefore
   carries a :class:`Ratio`, and every ratio row the sheet publishes is on
   :data:`SHEET_RATIO_FIELDS`, which the roll-up refuses to sum.
3. **Missing means missing.** A field absent from the sheet stays absent through
   the roll-up and reaches the renderer as ``None`` — never 0, never a default.
   A period total is emitted only when *every* month in the period reports the
   field, so two thirds of a quarter is never published as the quarter.

Deliberately importing neither ``snapshots`` (its routes are WORKSPACE_SHARED;
pulling it in would drag that scoping into a TENANT_SCOPED report path) nor
``pdf_export`` (a different visual identity that is not being extended).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Sequence

#: Bumped whenever a change here would produce different numbers or rows for
#: the same inputs. It is part of the idempotency key, so a bump re-derives
#: rather than serving a stale roll-up.
GENERATOR_VERSION = "mr-board-report/2"

MONEY, PCT, INT = "money", "pct", "int"
FORMATS = frozenset({MONEY, PCT, INT})

UP, DOWN, NEUTRAL = "up", "down", "neutral"
POLARITIES = frozenset({UP, DOWN, NEUTRAL})

#: The seven bands, in the order the report prints them.
GROUPS = (
    "Budget & Efficiency",
    "Projected — Actualized",
    "Revenue — Actualized",
    "Projected — Not Actualized",
    "Paying — Not Actualized",
    "Inbound Sales Pipeline",
    "Goal & Blended Financials",
)

#: Marker in the first slot of a ledger row that introduces a group band —
#: the template's ``const G='group'``.
GROUP = "group"

#: The report's CAC. Named once, here, so grep finds every use of it. NOT
#: ``cac``: see rule 1 in the module docstring.
CAC_KEY = "cac_per_revenue_client"

#: Ratio rows the roll-up tab publishes per month. Summing or averaging any of
#: them across months is arithmetically wrong, so the roll-up never reads them —
#: it recomputes the published metric from the components instead. Keeping the
#: sheet's ``cac`` here is also what makes the CAC collision structurally
#: impossible rather than a naming convention people have to remember.
SHEET_RATIO_FIELDS = frozenset({
    "qualified_lead_ratio_pct", "cost_per_lead", "cost_per_qualified_lead",
    "cost_per_demo_booked", "cost_per_qual_demo_booked", "cost_per_demo_completed",
    "show_up_rate_pct", "leads_to_demo_booked_pct", "leads_to_qual_demo_booked_pct",
    "revenue_target_pct", "average_deal_amount", "conversion_rate_pct",
    "roas_pct", "roas_not_actualized_pct", "cac",
})


@dataclass(frozen=True)
class Ratio:
    """How a derived metric is rebuilt from period-summed components.

    ``numerator`` and ``denominator`` name additive sheet fields — counts and
    amounts — and are summed over the period *first*, then divided. ``scale`` is
    100 for a percentage, so the result is comparable with the sheet's own
    "188.92%" cells, and 1 for a per-unit cost.
    """

    numerator: str
    denominator: str
    scale: float = 1.0
    digits: int = 2

    def components(self) -> tuple[str, str]:
        return (self.numerator, self.denominator)

    def apply(self, values: Mapping[str, float]) -> float | None:
        """The metric, or ``None`` when a component is missing or the
        denominator is zero — there is no honest ratio in either case."""
        n = values.get(self.numerator)
        d = values.get(self.denominator)
        if n is None or not d:
            return None
        return round(n / d * self.scale, self.digits)


@dataclass(frozen=True)
class Metric:
    """One published row of the report, described as data.

    Exactly one of ``source`` (read the sheet field straight, summed over the
    period) and ``formula`` (recompute from summed components) is set.

    ``basis`` is the plain-language note the renderer prints under the row when
    the number cannot be reconciled from the rows next to it — currently
    Conversion Rate, whose denominator is the Direct completed count and not the
    all-in one printed above it, and CAC, whose neighbour is a different cost.
    """

    key: str
    label: str
    group: str
    format: str
    polarity: str
    source: str | None = None
    formula: Ratio | None = None
    basis: str | None = None

    @property
    def derived(self) -> bool:
        return self.formula is not None

    def value(self, components: Mapping[str, float]) -> float | None:
        if self.formula is not None:
            return self.formula.apply(components)
        v = components.get(self.source)  # type: ignore[arg-type]
        return None if v is None else round(v, 2)


def _m(key, label, group, fmt, polarity, *, source=None, formula=None, basis=None) -> Metric:
    return Metric(key=key, label=label, group=group, format=fmt, polarity=polarity,
                  source=source, formula=formula, basis=basis)


_G1, _G2, _G3, _G4, _G5, _G6, _G7 = GROUPS

#: The report, as data. Order is the print order; the group bands fall out of
#: ``Metric.group`` so a row cannot end up under the wrong heading.
#:
#: Polarity decouples direction from goodness: ``good = (up and d>0) or
#: (down and d<0)``. Spend down is green, CAC down is green, Qualified Leads
#: down is red, and Budget and the revenue goal are never coloured at all — they
#: are inputs the team chose, not outcomes it earned.
CATALOG: tuple[Metric, ...] = (
    # --- Budget & Efficiency -------------------------------------------------
    _m("budget", "Budget", _G1, MONEY, NEUTRAL, source="budget"),
    _m("spend", "Spend", _G1, MONEY, DOWN, source="spend"),
    _m("leads", "Leads", _G1, INT, UP, source="leads"),
    _m("qualified_leads", "Qualified Leads", _G1, INT, UP, source="qualified_leads"),
    _m("qualified_lead_ratio_pct", "Qualified Lead Ratio", _G1, PCT, UP,
       formula=Ratio("qualified_leads", "leads", 100)),
    _m("cost_per_lead", "Cost per Lead", _G1, MONEY, DOWN,
       formula=Ratio("spend", "leads")),
    _m("cost_per_qualified_lead", "Cost per Qualified Lead", _G1, MONEY, DOWN,
       formula=Ratio("spend", "qualified_leads")),
    _m("qual_demos_booked", "Qualified Demos Booked (SDR+VAPI+Direct)", _G1, INT, UP,
       source="qual_demos_booked"),
    _m("demos_completed", "Total Demos Completed (SDR+VAPI+Direct)", _G1, INT, UP,
       source="demos_completed"),
    # Not in the marketing team's template — added on the user's decision,
    # because Conversion Rate below is computed on THIS count and two adjacent
    # cards a reader cannot reconcile will not ship. 48/248 = 19.35%, the
    # published figure; 48/272 = 17.65%, which appears nowhere.
    _m("demos_completed_direct", "Total Demos Completed (Direct)", _G1, INT, UP,
       source="demos_completed_direct",
       basis="the Conversion Rate denominator — Direct completions only"),
    _m("show_up_rate_pct", "Show-up Rate (SDR+VAPI+Direct)", _G1, PCT, UP,
       formula=Ratio("demos_completed", "qual_demos_booked", 100)),
    _m("lost_dnc_bad_lead", "Lost DNC (Bad Lead)", _G1, INT, DOWN,
       source="lost_dnc_bad_lead"),
    _m("cost_per_qual_demo_booked", "Cost / Qualified Demo Booked (SDR+VAPI+Direct)",
       _G1, MONEY, DOWN, formula=Ratio("spend", "qual_demos_booked")),
    # The template calls this "Cost / Qualified Demo Completed", but 239,581.57 /
    # 880.81 = 272.0 — the ALL-IN completed count. It is the sheet's plain
    # cost-per-demo-completed row, and the denominator here says so.
    _m("cost_per_demo_completed", "Cost / Qualified Demo Completed (SDR+VAPI+Direct)",
       _G1, MONEY, DOWN, formula=Ratio("spend", "demos_completed")),
    # --- Projected — Actualized ---------------------------------------------
    _m("projected_new_clients", "Number of Projected New Clients (Actualized)",
       _G2, INT, UP, source="projected_new_clients"),
    _m("projected_services_sold", "Total Projected Services Sold (Actualized)",
       _G2, INT, UP, source="projected_services_sold"),
    _m("projected_amount_sold", "Projected Total Amount Sold ($) (Actualized)",
       _G2, MONEY, UP, source="projected_amount_sold"),
    _m("projected_mrr_without_setup_fee", "Projected MRR w/o Setup Fees (Actualized)",
       _G2, MONEY, UP, source="projected_mrr_without_setup_fee"),
    # --- Revenue — Actualized ------------------------------------------------
    _m("revenue_clients", "Number of Revenue Clients (Actualized)", _G3, INT, UP,
       source="revenue_clients"),
    _m("services_sold", "Total Services Sold (Actualized)", _G3, INT, UP,
       source="services_sold"),
    _m("revenue_amount_sold", "Revenue Amount Sold (Actualized)", _G3, MONEY, UP,
       source="revenue_amount_sold"),
    _m("revenue_amount_sold_without_setup_fee",
       "Revenue Amount Sold w/o Setup Fee (Actualized)", _G3, MONEY, UP,
       source="revenue_amount_sold_without_setup_fee"),
    # --- Projected — Not Actualized (sheet occurrence 1) ---------------------
    _m("projected_new_clients_not_actualized",
       "Number of Projected New Clients (Not Actualized)", _G4, INT, UP,
       source="projected_new_clients_not_actualized"),
    _m("services_sold_not_actualized", "Total Services Sold (Not Actualized)",
       _G4, INT, UP, source="services_sold_not_actualized"),
    _m("revenue_amount_sold_not_actualized", "Revenue Amount Sold ($) (Not Actualized)",
       _G4, MONEY, UP, source="revenue_amount_sold_not_actualized"),
    _m("revenue_amount_sold_without_setup_fee_not_actualized",
       "Revenue Amount Sold w/o Setup Fee (Not Actualized)", _G4, MONEY, UP,
       source="revenue_amount_sold_without_setup_fee_not_actualized"),
    # --- Paying — Not Actualized (sheet occurrence 2 of the same labels) -----
    _m("paying_new_clients", "Number of Paying New Clients (Not Actualized)",
       _G5, INT, UP, source="paying_new_clients"),
    _m("paying_services_sold", "Total Services Sold (Not Actualized)", _G5, INT, UP,
       source="paying_services_sold"),
    _m("paying_revenue_amount_sold", "Revenue Amount Sold (Not Actualized)",
       _G5, MONEY, UP, source="paying_revenue_amount_sold"),
    _m("paying_revenue_amount_sold_without_setup_fee",
       "Revenue Amount Sold w/o Setup Fee (Not Actualized)", _G5, MONEY, UP,
       source="paying_revenue_amount_sold_without_setup_fee"),
    # --- Inbound Sales Pipeline ---------------------------------------------
    _m("inbound_pipeline_revenue_amount_sold",
       "Revenue Amount Sold (Inbound Sales Pipeline)", _G6, MONEY, UP,
       source="inbound_pipeline_revenue_amount_sold"),
    # --- Goal & Blended Financials -------------------------------------------
    _m("revenue_target_pct", "% of Revenue Target Goal (Not Actualized)", _G7, PCT, UP,
       formula=Ratio("revenue_amount_sold_not_actualized", "revenue_sold_goal", 100)),
    # The goal MOVES between periods (480,000 -> 535,000) and is read per period,
    # summed across the period's months. A constant here would misstate one of
    # every two periods.
    _m("revenue_sold_goal", "Revenue Sold Goal Amount", _G7, MONEY, NEUTRAL,
       source="revenue_sold_goal"),
    _m("average_deal_amount", "Average Deal Amount", _G7, MONEY, UP,
       formula=Ratio("projected_amount_sold", "projected_new_clients")),
    _m("conversion_rate_pct", "Conversion Rate (%)", _G7, PCT, UP,
       formula=Ratio("revenue_clients", "demos_completed_direct", 100),
       basis="revenue clients / demos completed (Direct)"),
    _m("roas_pct", "ROAS — Actualized (%)", _G7, PCT, UP,
       formula=Ratio("revenue_amount_sold", "spend", 100)),
    _m("roas_not_actualized_pct", "ROAS — Not Actualized (%)", _G7, PCT, UP,
       formula=Ratio("revenue_amount_sold_not_actualized", "spend", 100)),
    _m(CAC_KEY, "CAC (Spend / Revenue Client)", _G7, MONEY, DOWN,
       formula=Ratio("spend", "revenue_clients"),
       basis="spend per actualized revenue client — not the cost per completed demo above"),
)

BY_KEY: dict[str, Metric] = {m.key: m for m in CATALOG}

#: Every sheet field the roll-up has to sum: the straight-through sources plus
#: every formula component. Derived by construction, so a catalog row can never
#: reference a field the roll-up forgot to total.
ADDITIVE_FIELDS: frozenset[str] = frozenset(
    [m.source for m in CATALOG if m.source]
    + [c for m in CATALOG if m.formula for c in m.formula.components()]
)

#: Everything that must never be summed or averaged across months — the
#: catalog's derived keys plus the sheet's own ratio rows. Exported because the
#: guard that matters lives outside this module: ``reports._OFFICIAL_TOTAL_FIELDS``
#: sums each of its fields across months, so the day a ratio is added to it, the
#: ratio gets summed. A test asserts the two sets stay disjoint.
RECOMPUTED_FIELDS: frozenset[str] = frozenset(
    m.key for m in CATALOG if m.derived
) | SHEET_RATIO_FIELDS

#: The fields the roll-up carries **per month** as well as summed, in the order
#: the by-month chart draws them: spend, actualized revenue sold, not-actualized
#: revenue sold.
#:
#: Deliberately three fields and not :data:`ADDITIVE_FIELDS`. The monthly cells
#: ride along in every :class:`ReportLedger` and therefore in every persisted
#: run, so this list is the ledger's weight: three fields x N months, not
#: thirty-odd. Adding one is a decision about storage, which is why it is a
#: named constant with a guard rather than a comprehension at the call site.
#:
#: Every entry must be an *additive* source field. A ratio has no honest monthly
#: cell to carry — recomputing it per month gives a number that does not
#: reconcile with the period ratio printed next to it, which is the averaging
#: defect rule 2 exists to prevent, one level down.
MONTHLY_FIELDS: tuple[str, ...] = (
    "spend",
    "revenue_amount_sold",
    "revenue_amount_sold_not_actualized",
)


def _groups_in_order(catalog: Sequence[Metric]) -> bool:
    """True when catalog rows appear in ``GROUPS`` order with no group split in
    two — the property that lets the renderer emit bands by walking the list."""
    seen: list[str] = []
    for m in catalog:
        if not seen or seen[-1] != m.group:
            if m.group in seen:
                return False
            seen.append(m.group)
    return seen == [g for g in GROUPS if g in seen]


def _validate_monthly(fields: Sequence[str] = MONTHLY_FIELDS,
                      additive: frozenset[str] = ADDITIVE_FIELDS,
                      recomputed: frozenset[str] = RECOMPUTED_FIELDS) -> None:
    """Fail at import if the by-month series stops being summable.

    Takes its three sets as arguments for the same reason
    :func:`_validate_catalog` does — the refusal is provable without reaching
    into module globals to break something.
    """
    seen: set[str] = set()
    for f in fields:
        if f in seen:
            raise ValueError(f"MONTHLY_FIELDS: duplicate field {f!r}")
        seen.add(f)
        if f in recomputed:
            raise ValueError(
                f"MONTHLY_FIELDS: {f!r} is recomputed from summed components, so it "
                "has no honest per-month cell — carry its components instead")
        if f not in additive:
            raise ValueError(
                f"MONTHLY_FIELDS: {f!r} is not a field the roll-up sums, so no month "
                "would ever report it")


def _validate_catalog(catalog: Sequence[Metric] = CATALOG) -> None:
    """Fail at import rather than on a board slide.

    Takes the catalog as an argument so the guards themselves are testable —
    a test proves the refusal by handing this a deliberately broken catalog,
    without reaching into module globals to do it.
    """
    seen: set[str] = set()
    for m in catalog:
        where = f"catalog entry {m.key!r}"
        if m.key in seen:
            raise ValueError(f"{where}: duplicate key")
        seen.add(m.key)
        if m.group not in GROUPS:
            raise ValueError(f"{where}: unknown group {m.group!r}")
        if m.format not in FORMATS:
            raise ValueError(f"{where}: unknown format {m.format!r}")
        if m.polarity not in POLARITIES:
            raise ValueError(f"{where}: unknown polarity {m.polarity!r}")
        if (m.source is None) == (m.formula is None):
            raise ValueError(f"{where}: set exactly one of source / formula")
        if m.source is not None and m.source in SHEET_RATIO_FIELDS:
            raise ValueError(
                f"{where}: reads the sheet ratio row {m.source!r} straight. A ratio "
                f"cannot be summed or averaged across months — give it a Ratio formula")
        for comp in (m.formula.components() if m.formula else ()):
            if comp in SHEET_RATIO_FIELDS:
                raise ValueError(
                    f"{where}: component {comp!r} is a sheet ratio row, not an "
                    f"additive count or amount")
    if "cac" in seen:
        raise ValueError(
            "catalog: 'cac' is ambiguous — CampaignMetric.cac is spend/demos_completed "
            f"and the report's CAC is spend/revenue_clients. Use {CAC_KEY!r}")
    cac = next((m for m in catalog if m.key == CAC_KEY), None)
    if cac is None:
        raise ValueError(f"catalog: the report's CAC row {CAC_KEY!r} is missing")
    if cac.formula is None or cac.formula.denominator != "revenue_clients":
        raise ValueError(
            f"catalog: {CAC_KEY!r} must divide by revenue_clients; a demos-completed "
            "denominator is the cost-per-completed-demo proxy, a different metric")
    if not _groups_in_order(catalog):
        raise ValueError("catalog: rows are not grouped in GROUPS order")


_validate_catalog()
_validate_monthly()


# --- periods ----------------------------------------------------------------

@dataclass(frozen=True)
class PeriodSpec:
    """A named set of month keys. Deliberately *not* a quarter.

    The template's ``q1c`` / ``q2c`` are only "column A" and "column B", so the
    same structure has to carry month-vs-month and year-vs-year. Anything that
    hardcodes three months forecloses both.
    """

    key: str
    label: str
    months: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.months:
            raise ValueError(f"period {self.key!r} has no months")
        if len(set(self.months)) != len(self.months):
            raise ValueError(f"period {self.key!r} repeats a month")

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "months": list(self.months)}


def months_of(year: int, first: int, count: int) -> tuple[str, ...]:
    """``count`` consecutive ``YYYY-MM`` keys from ``first``, rolling the year."""
    out: list[str] = []
    y, m = year, first
    for _ in range(count):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return tuple(out)


def month_period(year: int, month: int, *, label: str | None = None) -> PeriodSpec:
    key = f"{year:04d}-{month:02d}"
    return PeriodSpec(key, label or key, (key,))


def quarter_period(year: int, quarter: int, *, label: str | None = None) -> PeriodSpec:
    """Convenience only — :func:`roll_up` itself takes any list of months."""
    if not 1 <= quarter <= 4:
        raise ValueError(f"quarter must be 1-4, got {quarter}")
    return PeriodSpec(f"{year:04d}-Q{quarter}", label or f"Q{quarter}",
                      months_of(year, quarter * 3 - 2, 3))


def year_period(year: int, *, label: str | None = None) -> PeriodSpec:
    return PeriodSpec(f"{year:04d}", label or str(year), months_of(year, 1, 12))


# --- roll-up ----------------------------------------------------------------

#: Per-channel fields the untracked reconciliation needs. Channel figures come
#: from the vendor tabs; the totals come from the roll-up tab, which also
#: aggregates ledger sources with no vendor tab at all — which is exactly why
#: the two do not agree and why the gap is a row rather than a footnote.
CHANNEL_FIELDS = ("spend", "revenue_amount_sold", "revenue_clients")

#: The label the reconciliation row prints under. Named once so the renderer,
#: the tests and any later export all say the same thing.
UNTRACKED_LABEL = "Other / untracked"


@dataclass(frozen=True)
class ChannelTotals:
    channel: str
    spend: float | None = None
    revenue: float | None = None
    clients: float | None = None

    @property
    def roas_pct(self) -> float | None:
        if self.revenue is None or not self.spend:
            return None
        return round(self.revenue / self.spend * 100, 2)

    @property
    def cac(self) -> float | None:
        """Spend per revenue client — the same definition as :data:`CAC_KEY`,
        never the cost-per-completed-demo proxy that shares the name elsewhere."""
        if self.spend is None or not self.clients:
            return None
        return round(self.spend / self.clients, 2)

    def as_dict(self) -> dict:
        return {"channel": self.channel, "spend": self.spend, "revenue": self.revenue,
                "clients": self.clients, "roas_pct": self.roas_pct, "cac": self.cac}


@dataclass(frozen=True)
class Untracked:
    """All-Sources totals minus what the tracked channels account for.

    Q1 leaves $16,881 (7.0%) and 8 clients unattributed, Q2 $22,512 (12.0%) and
    12 — a gap that is growing, which is the whole reason it is published as a
    row instead of a footnote.
    """

    spend: float | None = None
    spend_pct: float | None = None
    revenue: float | None = None
    revenue_pct: float | None = None
    clients: float | None = None
    clients_pct: float | None = None

    def as_dict(self) -> dict:
        return {"spend": self.spend, "spend_pct": self.spend_pct,
                "revenue": self.revenue, "revenue_pct": self.revenue_pct,
                "clients": self.clients, "clients_pct": self.clients_pct}


@dataclass(frozen=True)
class MonthCell:
    """One month of a period, carried through uncollapsed for the chart.

    ``values`` holds only the :data:`MONTHLY_FIELDS` this month actually
    reported. A field the month did not report is simply not a key — the same
    absent-is-not-zero rule the period totals follow, one level down.

    Every month of the period gets a cell, including a month that reported
    nothing at all. Dropping an empty month would silently shorten the chart's
    axis, and a quarter drawn as two bars reads as a complete two-month period
    rather than as the partially-covered quarter it is.
    """

    month: str
    values: Mapping[str, float]

    def value(self, field: str) -> float | None:
        return self.values.get(field)

    def as_dict(self) -> dict:
        return {"month": self.month, "values": dict(self.values)}


@dataclass(frozen=True)
class PeriodRollup:
    """One report column: a period's totals, its channels, and what is missing."""

    period: PeriodSpec
    components: Mapping[str, float]
    values: Mapping[str, float]
    channels: tuple[ChannelTotals, ...] = ()
    untracked: Untracked | None = None
    gaps: tuple[str, ...] = ()
    #: One cell per month of ``period.months``, in period order, carrying only
    #: :data:`MONTHLY_FIELDS`. Never re-ordered and never filtered: the chart's
    #: axis is this tuple.
    monthly: tuple[MonthCell, ...] = ()

    def value(self, key: str) -> float | None:
        return self.values.get(key)

    def as_dict(self) -> dict:
        return {
            "period": self.period.as_dict(),
            "values": dict(self.values),
            "components": dict(self.components),
            "channels": [c.as_dict() for c in self.channels],
            "untracked": self.untracked.as_dict() if self.untracked else None,
            "gaps": list(self.gaps),
            "monthly": [c.as_dict() for c in self.monthly],
        }


def _sum_over(months: Sequence[str], per_month: Mapping[str, Mapping[str, float]],
              fields: Iterable[str], *, what: str, gaps: list[str]) -> dict[str, float]:
    """Total each field over ``months`` — only when every month reports it.

    A field present in two of three months is not two thirds of a quarter, it is
    an unknown quarter. Summing whatever happens to be there produces a number
    wrong by an amount nobody can see, so the field is dropped and the months
    that were missing are named.
    """
    out: dict[str, float] = {}
    for f in sorted(fields):
        missing = [mk for mk in months if (per_month.get(mk) or {}).get(f) is None]
        if missing:
            if len(missing) < len(months):
                gaps.append(
                    f"{what}: '{f}' is missing for {', '.join(missing)} — the period "
                    f"total is withheld rather than summed from a subset of months")
            continue
        out[f] = round(sum(float(per_month[mk][f]) for mk in months), 2)
    return out


def _month_cells(months: Sequence[str],
                 per_month: Mapping[str, Mapping[str, float]]) -> tuple[MonthCell, ...]:
    """The period's months, uncollapsed, carrying :data:`MONTHLY_FIELDS` only.

    Unlike :func:`_sum_over` this withholds nothing, because there is no subset
    here to be misread as a whole: a month reported a field or it did not. The
    withheld *period total* and the present *monthly cells* coexist on purpose —
    that pair is what makes a partially-covered period look partial rather than
    look like a shorter period that is complete.
    """
    out: list[MonthCell] = []
    for mk in months:
        row = per_month.get(mk) or {}
        out.append(MonthCell(
            month=mk,
            values={f: round(float(row[f]), 2) for f in MONTHLY_FIELDS
                    if row.get(f) is not None},
        ))
    return tuple(out)


def _share(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(part / whole * 100, 2)


def roll_up(
    period: PeriodSpec,
    official: Mapping[str, Mapping[str, float]],
    *,
    channels: Mapping[str, Mapping[str, Mapping[str, float]]] | None = None,
) -> PeriodRollup:
    """Collapse the roll-up tab's monthly official figures into one period.

    ``official`` is :func:`sources.sheets_source.parse_official_totals` output —
    ``{"YYYY-MM": {field: value}}``. ``channels`` is the same shape one level
    deeper, ``{channel: {"YYYY-MM": {spend, revenue_amount_sold, revenue_clients}}}``,
    and is what makes the untracked reconciliation computable; omit it and the
    reconciliation is absent rather than guessed at.

    Additive fields are summed. Ratios are **recomputed from those sums** — never
    averaged across months, which measures +1.74pp of error on ROAS for this
    data. Anything a month does not report stays missing all the way out.

    The period's months also come through uncollapsed in ``monthly``, carrying
    :data:`MONTHLY_FIELDS` and nothing else — the by-month chart's own data,
    which the period totals cannot reconstruct.
    """
    gaps: list[str] = []
    components = _sum_over(period.months, official, ADDITIVE_FIELDS,
                           what=period.key, gaps=gaps)
    values: dict[str, float] = {}
    for m in CATALOG:
        v = m.value(components)
        if v is not None:
            values[m.key] = v

    channel_totals: list[ChannelTotals] = []
    for name in sorted(channels or {}):
        c = _sum_over(period.months, channels[name], CHANNEL_FIELDS,
                      what=f"{period.key}/{name}", gaps=gaps)
        channel_totals.append(ChannelTotals(
            channel=name, spend=c.get("spend"), revenue=c.get("revenue_amount_sold"),
            clients=c.get("revenue_clients")))

    untracked = None
    if channel_totals:
        untracked, more = _reconcile_untracked(period, components, channel_totals)
        gaps.extend(more)

    return PeriodRollup(period=period, components=components, values=values,
                        channels=tuple(channel_totals), untracked=untracked,
                        gaps=tuple(gaps),
                        monthly=_month_cells(period.months, official))


def _reconcile_untracked(
    period: PeriodSpec, components: Mapping[str, float],
    channels: Sequence[ChannelTotals],
) -> tuple[Untracked, list[str]]:
    """The All-Sources total minus the tracked channels, per measure."""
    gaps: list[str] = []
    out: dict[str, float | None] = {}
    pairs = (("spend", "spend", "spend"),
             ("revenue", "revenue_amount_sold", "revenue"),
             ("clients", "revenue_clients", "clients"))
    for name, official_field, attr in pairs:
        total = components.get(official_field)
        parts = [getattr(c, attr) for c in channels]
        if total is None or any(p is None for p in parts):
            out[name] = None
            out[f"{name}_pct"] = None
            continue
        gap = round(total - sum(parts), 2)
        out[name] = gap
        out[f"{name}_pct"] = _share(gap, total)
        if gap < 0:
            gaps.append(
                f"{period.key}: the tracked channels report more {name} than the "
                f"All-Sources total ({sum(parts):,.2f} vs {total:,.2f}) — that is a "
                f"block being counted twice, not an untracked surplus")
    return Untracked(**out), gaps  # type: ignore[arg-type]


# --- two-period comparison --------------------------------------------------

@dataclass(frozen=True)
class LedgerRow:
    """One published row across two periods.

    :meth:`to_ledger` is the renderer's contract — the template's
    ``[label, valueA, valueB, format, polarity]``. Everything else on this
    dataclass is for tracing, not for rendering.
    """

    key: str
    label: str
    group: str
    format: str
    polarity: str
    a: float | None
    b: float | None
    basis: str | None = None

    def to_ledger(self) -> list:
        return [self.label, self.a, self.b, self.format, self.polarity]

    @property
    def delta(self) -> float | None:
        if self.a is None or self.b is None:
            return None
        return round(self.b - self.a, 2)

    @property
    def change_pct(self) -> float | None:
        if self.a is None or self.b is None or not self.a:
            return None
        return round((self.b - self.a) / self.a * 100, 2)

    @property
    def improved(self) -> bool | None:
        """Direction *and* goodness — the reason polarity exists. ``None`` when
        the row is uncoloured: neutral polarity, no change, or a missing value."""
        d = self.delta
        if self.polarity == NEUTRAL or d is None or d == 0:
            return None
        return (self.polarity == UP and d > 0) or (self.polarity == DOWN and d < 0)

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "group": self.group,
                "format": self.format, "polarity": self.polarity,
                "a": self.a, "b": self.b, "basis": self.basis,
                "delta": self.delta, "change_pct": self.change_pct,
                "improved": self.improved}


@dataclass(frozen=True)
class ChannelRow:
    channel: str
    a: ChannelTotals | None
    b: ChannelTotals | None

    def as_dict(self) -> dict:
        pick = lambda t, f: getattr(t, f) if t else None
        return {"channel": self.channel,
                "spend_a": pick(self.a, "spend"), "spend_b": pick(self.b, "spend"),
                "revenue_a": pick(self.a, "revenue"), "revenue_b": pick(self.b, "revenue"),
                "roas_a": pick(self.a, "roas_pct"), "roas_b": pick(self.b, "roas_pct"),
                "cac_a": pick(self.a, "cac"), "cac_b": pick(self.b, "cac")}


@dataclass(frozen=True)
class ReportLedger:
    """Everything the renderer needs and nothing it has to interpret."""

    columns: tuple[str, str]
    periods: tuple[PeriodSpec, PeriodSpec]
    rows: tuple[LedgerRow, ...]
    channels: tuple[ChannelRow, ...] = ()
    untracked: tuple[Untracked | None, Untracked | None] = (None, None)
    gaps: tuple[str, ...] = ()
    cache_key: str = ""
    #: Column A's months, then column B's, each in period order. Two tuples and
    #: not one merged list: which column a month belongs to is the renderer's
    #: business, and a single-period ledger is the same period twice, so merging
    #: here would draw every month of it twice.
    monthly: tuple[tuple[MonthCell, ...], tuple[MonthCell, ...]] = ((), ())

    def to_r_array(self) -> list[list]:
        """The template's ``R[]``: group bands interleaved with metric rows."""
        out: list[list] = []
        current = None
        for row in self.rows:
            if row.group != current:
                out.append([GROUP, row.group])
                current = row.group
            out.append(row.to_ledger())
        return out

    def as_dict(self) -> dict:
        return {
            "generator": GENERATOR_VERSION,
            "cache_key": self.cache_key,
            "columns": list(self.columns),
            "periods": [p.as_dict() for p in self.periods],
            "groups": [g for g in GROUPS if any(r.group == g for r in self.rows)],
            "rows": [r.as_dict() for r in self.rows],
            "channels": [c.as_dict() for c in self.channels],
            "untracked": [u.as_dict() if u else None for u in self.untracked],
            "gaps": list(self.gaps),
            "monthly": [[c.as_dict() for c in side] for side in self.monthly],
        }


def compare(a: PeriodRollup, b: PeriodRollup, *,
            captured_on: date | None = None) -> ReportLedger:
    """Lay two period roll-ups side by side in the report's ledger shape.

    ``a`` is column A and ``b`` is column B — nothing here knows or cares that
    the template's two columns happened to be quarters.
    """
    rows = tuple(
        LedgerRow(key=m.key, label=m.label, group=m.group, format=m.format,
                  polarity=m.polarity, a=a.value(m.key), b=b.value(m.key),
                  basis=m.basis)
        for m in CATALOG
    )
    a_names = {c.channel for c in a.channels}
    names = [c.channel for c in a.channels] + [
        c.channel for c in b.channels if c.channel not in a_names]
    by_a = {c.channel: c for c in a.channels}
    by_b = {c.channel: c for c in b.channels}
    channels = [ChannelRow(n, by_a.get(n), by_b.get(n)) for n in names]
    if a.untracked or b.untracked:
        channels.append(ChannelRow(
            UNTRACKED_LABEL,
            _untracked_as_channel(a.untracked),
            _untracked_as_channel(b.untracked)))
    return ReportLedger(
        columns=(a.period.label, b.period.label),
        periods=(a.period, b.period),
        rows=rows,
        channels=tuple(channels),
        untracked=(a.untracked, b.untracked),
        gaps=tuple(a.gaps) + tuple(b.gaps),
        cache_key=cache_key([a.period, b.period], captured_on) if captured_on else "",
        monthly=(a.monthly, b.monthly),
    )


def _untracked_as_channel(u: Untracked | None) -> ChannelTotals | None:
    if u is None:
        return None
    return ChannelTotals(UNTRACKED_LABEL, spend=u.spend, revenue=u.revenue,
                         clients=u.clients)


# --- idempotency ------------------------------------------------------------

def cache_key(periods: Sequence[PeriodSpec], captured_on: date,
              *, version: str = GENERATOR_VERSION) -> str:
    """Stable id for "this report, from this data capture, by this generator".

    Same periods + same capture date + same generator version gives the same
    key, so a repeated request serves what was already derived instead of
    re-deriving it. A change to any of the three produces a different key, which
    is what makes a :data:`GENERATOR_VERSION` bump a cache invalidation rather
    than a stale read. Persistence itself lands in step 3, via the shared store.
    """
    payload = json.dumps(
        {"version": version, "captured_on": captured_on.isoformat(),
         "periods": [p.as_dict() for p in periods]},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
