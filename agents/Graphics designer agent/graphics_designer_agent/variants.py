"""UI-facing metadata: variant concept cards, fonts, aspect ratios, brand kit.

None of the copy here is ever injected into a prompt (spec §5.2). It exists only
to render the studio UI. Prompt content lives exclusively in ``./prompts``.
"""

from __future__ import annotations

from .tokens import ASPECT_RATIOS, DEFAULT_FONT

# ── Locked brand system (spec §2.2 / §2.3) — read-only in the UI ──────────────
LOCKED_COLORS = {
    "gradient": ["#FFFFFF", "#BDCFED", "#A2C0E6", "#1746A2"],
    "text": "#0F0F0F",
    "accent": "#85AEFD",
    "headline_highlight": {"from": "#86AFFE", "to": "#2653AB", "direction": "left-to-right linear"},
    "cta": {
        "from": "#FF8A3D",
        "to": "#F26A1A",
        "direction": "135° diagonal",
        "shadow": "rgba(242, 106, 26, 0.25) 0 8px 20px",
    },
}

# Authored verbatim (spec §2.3) — display exactly as-is.
BRAND_KIT_BLOCK = (
    "{\n"
    "Vertical gradient right side : #BDCFED TO #1746A2\n"
    "Vertical gradient left side : #FFFFFF TO #1746A2\n"
    "Horizontal gradient top left to top right : #FFFFFF TO #A2C0E6\n"
    "Horizontal gradient bottom left to bottom right : #1746A2 TO #1746A2\n"
    "}"
)

SOURCE_NOTE_STAGE1 = (
    'note strict only use these only I am also specifying the brand kit colors '
    "for reference"
)

# ── Stage 1 — gradient base ───────────────────────────────────────────────────
STAGE1_VARIANTS = [
    {
        "id": "A",
        "prompt_file": "stage1_gradient_A.txt",
        "title": "Diagonal Sweep",
        "desc": "White top-left flowing into deep royal blue bottom-right.",
        "css_gradient": "linear-gradient(135deg, #FFFFFF 0%, #BDCFED 35%, #A2C0E6 55%, #1746A2 100%)",
    },
    {
        "id": "B",
        "prompt_file": "stage1_gradient_B.txt",
        "title": "Inverted Horizon",
        "desc": "Royal blue across the top dissolving to pure white at the bottom.",
        "css_gradient": "linear-gradient(180deg, #1746A2 0%, #A2C0E6 55%, #BDCFED 75%, #FFFFFF 100%)",
    },
    {
        "id": "C",
        "prompt_file": "stage1_gradient_C.txt",
        "title": "Blue Duotone Library",
        "desc": "Office/law-library photo under a blue duotone, densest bottom-left, thinning upper-right.",
        "css_gradient": "linear-gradient(45deg, #1746A2 0%, #1746A2 35%, #A2C0E6 75%, #BDCFED 100%)",
    },
    {
        "id": "D",
        "prompt_file": "stage1_gradient_D.txt",
        "title": "High-Key Skyline",
        "desc": "Airy white-to-pale-blue sky with a washed-out cityscape silhouette below.",
        "css_gradient": "linear-gradient(180deg, #FFFFFF 0%, #FFFFFF 55%, #BDCFED 100%)",
    },
    {
        "id": "E",
        "prompt_file": "stage1_gradient_E.txt",
        "title": "Foggy Dissolve Skyline",
        "desc": "Overexposed white haze, top two-thirds, with a skyline dissolving upward into fog.",
        "css_gradient": "linear-gradient(180deg, #FFFFFF 0%, #FFFFFF 62%, #BDCFED 100%)",
    },
    {
        "id": "F",
        "prompt_file": "stage1_gradient_F.txt",
        "title": "Cinematic Night Vegas",
        "desc": "Dark navy overlay over a nighttime aerial cityscape, heavy edge vignette, warm lights glowing through.",
        "css_gradient": "radial-gradient(ellipse at 50% 50%, #2653AB 0%, #1746A2 60%, #0E2A5E 100%)",
    },
    {
        "id": "G",
        "prompt_file": "stage1_gradient_G.txt",
        "title": "Chevron Facets Sweep",
        "desc": "Pale upper-left deepening to solid blue right, with faint angular chevron facets embedded.",
        "css_gradient": "linear-gradient(120deg, #FFFFFF 0%, #BDCFED 30%, #A2C0E6 55%, #1746A2 100%)",
    },
    {
        "id": "H",
        "prompt_file": "stage1_gradient_H.txt",
        "title": "Sandwiched Duotone Cityscape",
        "desc": "Flat deep-blue top and bottom bands sandwiching a blue-duotone cityscape mid-section.",
        "css_gradient": "linear-gradient(180deg, #1746A2 0%, #3A66B5 45%, #1746A2 100%)",
    },
    {
        "id": "I",
        "prompt_file": "stage1_gradient_I.txt",
        "title": "Diagonal Chevron Blend",
        "desc": "White-left to medium-deep-blue-right sweep with faint chevron shapes in the blue zone.",
        "css_gradient": "linear-gradient(90deg, #FFFFFF 0%, #BDCFED 35%, #1746A2 100%)",
    },
    {
        "id": "J",
        "prompt_file": "stage1_gradient_J.txt",
        "title": "Monochrome Cityscape Fade",
        "desc": "Deep blue top fading lighter over a monochrome-blue cityscape, bottom band darkening back.",
        "css_gradient": "linear-gradient(180deg, #1746A2 0%, #A2C0E6 42%, #1746A2 100%)",
    },
    {
        "id": "K",
        "prompt_file": "stage1_gradient_K.txt",
        "title": "Aerial Map Sky",
        "desc": "Vertical sky from rich blue to luminous cyan, with cloud wisps and a faint street-map line network.",
        "css_gradient": "linear-gradient(180deg, #1746A2 0%, #A2C0E6 100%)",
    },
    {
        "id": "L",
        "prompt_file": "stage1_gradient_L.txt",
        "title": "Ghosted Office Wash",
        "desc": "Near-solid blue wash over a faded office/library interior, subtle radial lift center-right.",
        "css_gradient": "radial-gradient(ellipse at 65% 50%, #2653AB 0%, #1746A2 70%)",
    },
]

# ── Stage 2 — element library ─────────────────────────────────────────────────
# Stage 2 adds ONE subject to the approved Stage-1 background. There is a single
# immutable prompt — ``stage2_element_blend.txt`` (spec §5.2) — whose job is to
# merge the provided background with a subject seamlessly. Each variant supplies
# only that ``subject`` (substituted into the ``[SUBJECT]`` token); ``title`` /
# ``desc`` are UI copy and ``category`` groups the picker + the agent explorer.
# No variant describes the background — that is owned entirely by Stage 1.
STAGE2_BLEND_PROMPT = "stage2_element_blend.txt"

STAGE2_VARIANTS = [
    {
        "id": "A",
        "title": "Solo Virtual Assistant",
        "desc": "Single warm female VA at a desk with a headset; lower frame, upper area open.",
        "angle": "efficiency / warmth",
        "category": "people",
        "subject": "A single warm, professional female virtual assistant in a smart blazer over a crisp blouse, wearing a slim black headset, seated at a tidy desk and looking slightly off-camera at her monitor as if on a client call, cinematic shallow depth of field. She occupies the lower portion of the frame; keep the upper area open.",
    },
    {
        "id": "B",
        "title": "Honeycomb Trio",
        "desc": "Three VA portraits as a hexagon cluster, upper-right; lower ⅔ open.",
        "angle": "social-proof / global talent",
        "category": "people",
        "subject": "A cluster of three professional virtual assistants shown as a tight honeycomb of hexagonal portraits, grouped in the upper-right corner and leaving the lower two-thirds open — diverse, friendly, vetted-global-talent feel.",
    },
    {
        "id": "C",
        "title": "Full-Figure Male VA",
        "desc": "Confident suited male VA on the right; left side open.",
        "angle": "authority",
        "category": "people",
        "subject": "A confident full-figure male virtual assistant in a tailored formal suit, calm assured posture, occupying the right side of the frame and leaving the left open.",
    },
    {
        "id": "D",
        "title": "Partner's Corner Office, 11:47 PM",
        "desc": "Empty late-night office — burnout storytelling, right side.",
        "angle": "pain-point",
        "category": "scene",
        "subject": "An empty late-night corner office told as a quiet story — a vacant desk and chair, a dim desk lamp, a few scattered case folders, and city lights through a window, NO people — evoking an 11:47 PM burnout moment, set into the right side of the frame.",
    },
    {
        "id": "E",
        "title": "Big Law Office Tower",
        "desc": "Prestigious legal architecture on the right; left open.",
        "angle": "authority",
        "category": "architecture",
        "subject": "A single prestigious Big Law office tower — modern-classical granite-and-glass architecture with bronze-framed windows, shot from a heroic upward angle at golden hour — occupying the right side of the frame.",
    },
    {
        "id": "F",
        "title": "Calm Filipina VA",
        "desc": "Half-body Filipina VA, low bun + slim headset; lower-right, vast negative space.",
        "angle": "efficiency / warmth",
        "category": "people",
        "subject": "Minimal half-body of a Filipina woman in her late 20s, soft low bun, plain stone-grey top, slim headset, seated calmly. 85mm, f/1.8, one soft window light from the side, gentle falloff. Subject lower-right, vast empty space. No clutter. Matte, restrained.",
    },
    {
        "id": "G",
        "title": "Contract Line & Pen",
        "desc": "Ultra-minimal 3D line with a matte fountain pen; lower-center, open frame.",
        "angle": "simplicity / clarity",
        "category": "object",
        "subject": "An ultra-minimal 3D rendering of a single clean horizontal line with one matte fountain pen resting at a slight angle above it, muted tones with one thin orange nib accent and one soft shadow — conveys contract simplicity. Lower-center, frame mostly open. No documents, no clutter.",
    },
    {
        "id": "H",
        "title": "Open Doorway",
        "desc": "Single matte doorway ajar with a thin orange light edge; lower-center, emptiness.",
        "angle": "opportunity / entry",
        "category": "object",
        "subject": "A minimal 3D rendering of a single matte doorway frame standing alone, slightly ajar, soft blue with one thin orange edge of light along the opening and one gentle shadow — conveys opportunity and entry. Object lower-center, vast emptiness around. No room, no glow flood, no clutter.",
    },
    {
        "id": "I",
        "title": "Activation Toggle",
        "desc": "Minimal 3D toggle switched on with a soft orange indicator; centered small.",
        "angle": "instant activation",
        "category": "object",
        "subject": "An ultra-minimal 3D rendering of a single matte toggle switch in the \"on\" position, muted blue track with one soft orange indicator and one diffused shadow — conveys instant activation. Centered small, vast breathing room. No labels, no glow.",
    },
    {
        "id": "J",
        "title": "Vietnamese VA Reading",
        "desc": "Half-body VA reading a single sheet; small in lower-right, frame mostly empty.",
        "angle": "focus / diligence",
        "category": "people",
        "subject": "Minimal half-body of a Vietnamese woman in her 30s, soft side-parted hair, plain stone-grey blouse, minimal headset, eyes lowered reading a single sheet held lightly in one hand. 85mm, f/1.8, one soft window light, gentle shadow falloff. Subject small in lower-right, frame mostly empty. Matte, fine grain, restrained.",
    },
    {
        "id": "K",
        "title": "Calm Handshake",
        "desc": "Macro of two hands in a handshake, arms dissolving to shadow; centered-low.",
        "angle": "trust / partnership",
        "category": "object",
        "subject": "A restrained close-up of two hands meeting in a calm handshake, plain navy sleeve and plain white cuff, soft single key light, the rest of the hands and arms dissolving gently into shadow. 90mm macro, f/2.8. Hands centered-low, broad empty space above. Photoreal, matte, premium.",
    },
    {
        "id": "L",
        "title": "Assured Black Woman VA",
        "desc": "Three-quarter portrait, serene assured expression; lower-right, neg space upper-left.",
        "angle": "authority / assurance",
        "category": "people",
        "subject": "Three-quarter portrait of a Black woman in her 30s, natural cropped curls, single small earring, plain deep-navy top, minimal headset, serene assured expression. 105mm, f/2.0, soft Rembrandt light, generous shadow side. Subject lower-right, abundant negative space upper-left. Fine detail, premium restraint.",
    },
    {
        "id": "M",
        "title": "Editorial Filipina Portrait",
        "desc": "Editorial portrait in a charcoal blazer, looking past camera; small lower-right.",
        "angle": "premium restraint",
        "category": "people",
        "subject": "A minimalist editorial portrait of a Filipina woman in her late 20s, natural skin texture, soft low bun, barely-there headset, wearing an unadorned charcoal blazer, calm closed-mouth expression looking just past camera. 85mm at f/1.8, single large soft window light, deep natural shadow falloff. Subject small in the lower-right quadrant, the majority of the frame intentionally empty. Matte finish, fine grain, restrained.",
    },
    {
        "id": "N",
        "title": "Tidy Workspace Flatlay",
        "desc": "Top-down — laptop, notebook + pen, glasses, espresso; cluster lower-right.",
        "angle": "organised productivity",
        "category": "flatlay",
        "subject": "A top-down flatlay of a tidy workspace: open silver laptop, leather notebook with a fountain pen resting diagonally, folded reading glasses, a small white espresso cup on a saucer, and a single paperclip. Soft overhead daylight, faint natural shadows. Objects arranged in the lower-right cluster, upper-left clear. Sharp focus, clean isolated objects.",
    },
    {
        "id": "O",
        "title": "Hazy Glass Skyline",
        "desc": "Wide glass skyscrapers fading into haze; skyline on the lower band, airy top.",
        "angle": "scale / authority",
        "category": "architecture",
        "subject": "A wide architectural cityscape of modern glass skyscrapers of varied heights fading into soft atmospheric haze, from a mid-rise vantage. No people, no logos, no birds. 24mm lens, deep focus, gentle aerial perspective. The skyline sits along the lower band, leaving airy space above. Clean horizon, no added sky fill.",
    },
    {
        "id": "P",
        "title": "Desk Collaboration Pair",
        "desc": "Two professionals reviewing a document; pair left, right side open for copy.",
        "angle": "collaboration / mentorship",
        "category": "people",
        "subject": "A photoreal half-body of two professionals at a desk: a seated older Caucasian man with grey hair and rimless glasses in a navy suit pointing at a document, and a standing younger Black woman in a burgundy blazer leaning in attentively, warm collaborative expressions. 50mm, f/2.8, soft balanced studio light. Pair grouped to the left, right side open for copy. Unified clean cutout, soft desk shadow.",
    },
    {
        "id": "Q",
        "title": "Signed Contract Flatlay",
        "desc": "Top-down signed contract, pen on the line, glasses, phone, coffee; lower-right.",
        "angle": "deal closed / trust",
        "category": "flatlay",
        "subject": "A top-down flatlay of a signed contract with a fountain pen resting on the signature line, a pair of folded glasses, a sleek smartphone face-down, and a white coffee cup on a saucer. Soft overhead daylight, faint natural shadows, subtle paper grain. Objects clustered lower-right, upper-left clear. Sharp isolated objects.",
    },
    {
        "id": "R",
        "title": "Blue-Hour City Towers",
        "desc": "Glass towers at blue hour with warm window lights; skyline lower band, airy top.",
        "angle": "prestige / ambition",
        "category": "architecture",
        "subject": "A wide cityscape of glass towers at blue hour with warm window lights beginning to glow, soft atmospheric depth, no people, no logos. 24mm, deep focus, gentle aerial perspective. The skyline runs along the lower band, leaving airy space above. Clean horizon, no added sky fill.",
    },
    {
        "id": "S",
        "title": "Focused Black Man Typing",
        "desc": "Corporate photo, man typing on a laptop mid-action; lower-left, upper-right empty.",
        "angle": "focused productivity",
        "category": "people",
        "subject": "A hyper-real corporate photo of a Black man in his late 20s, short fade haircut, neat stubble, wearing a navy quarter-zip over a light-blue collared shirt, typing on an open silver laptop, captured mid-action looking down at the screen with a focused expression, shoulders angled 30° to camera. 50mm, f/2.8, bright even softbox lighting. Subject and laptop sit lower-left, upper-right kept empty. Precise edge mask, faint contact shadow under the forearms.",
    },
]

# Stable category order for grouped UI rendering + the element explorer.
STAGE2_CATEGORIES = ["people", "object", "flatlay", "architecture", "scene"]

# ── Stage 3 — fonts (spec §6.1) ───────────────────────────────────────────────
# The creative font is LOCKED to a single brand family: Causten. Users may pick
# any of its variations (weight + upright/oblique), but never a different family.
# The .otf files live in ``<agent>/Causten Font Family`` and are the canonical
# reference for what "Causten <variant>" means on a creative.
FONT_FAMILY = "Causten"

# Ordered Thin → Black; each weight in upright then oblique. ``file`` is the
# matching face under the Causten Font Family folder.
FONT_VARIANTS = [
    {"name": "Causten Thin", "weight": 100, "style": "normal", "file": "Causten-Thin.otf"},
    {"name": "Causten Thin Oblique", "weight": 100, "style": "oblique", "file": "Causten-ThinOblique.otf"},
    {"name": "Causten ExtraLight", "weight": 200, "style": "normal", "file": "Causten-ExtraLight.otf"},
    {"name": "Causten ExtraLight Oblique", "weight": 200, "style": "oblique", "file": "Causten-ExtraLightOblique.otf"},
    {"name": "Causten Light", "weight": 300, "style": "normal", "file": "Causten-Light.otf"},
    {"name": "Causten Light Oblique", "weight": 300, "style": "oblique", "file": "Causten-LightOblique.otf"},
    {"name": "Causten Regular", "weight": 400, "style": "normal", "file": "Causten-Regular.otf"},
    {"name": "Causten Regular Oblique", "weight": 400, "style": "oblique", "file": "Causten-RegularOblique.otf"},
    {"name": "Causten Medium", "weight": 500, "style": "normal", "file": "Causten-Medium.otf"},
    {"name": "Causten Medium Oblique", "weight": 500, "style": "oblique", "file": "Causten-MediumOblique.otf"},
    {"name": "Causten SemiBold", "weight": 600, "style": "normal", "file": "Causten-SemiBold.otf"},
    {"name": "Causten SemiBold Oblique", "weight": 600, "style": "oblique", "file": "Causten-SemiBoldOblique.otf"},
    {"name": "Causten Bold", "weight": 700, "style": "normal", "file": "Causten-Bold.otf"},
    {"name": "Causten Bold Oblique", "weight": 700, "style": "oblique", "file": "Causten-BoldOblique.otf"},
    {"name": "Causten ExtraBold", "weight": 800, "style": "normal", "file": "Causten-ExtraBold.otf"},
    {"name": "Causten ExtraBold Oblique", "weight": 800, "style": "oblique", "file": "Causten-ExtraBoldOblique.otf"},
    {"name": "Causten Black", "weight": 900, "style": "normal", "file": "Causten-Black.otf"},
    {"name": "Causten Black Oblique", "weight": 900, "style": "oblique", "file": "Causten-BlackOblique.otf"},
]

# Flat list of selectable names (Thin → Black), consumed by the UI dropdown. The
# default selection is carried by the run config, not by list position.
FONTS = [v["name"] for v in FONT_VARIANTS]
assert DEFAULT_FONT in FONTS, "DEFAULT_FONT must be one of the Causten variants"

# ── Stage 3 — text & CTA placement (§6.4) ─────────────────────────────────────
# The user picks where the text block and the CTA button sit. Each option maps to
# a descriptive phrase substituted into the Stage-3 prompt's placement tokens.
# Defaults reproduce the original left-aligned layout.
TEXT_PLACEMENTS = [
    {"key": "left", "label": "Left",
     "phrase": "the LEFT side of the image — a column occupying roughly the left 40% of the width, vertically centered, leaving the rest of the image clear"},
    {"key": "right", "label": "Right",
     "phrase": "the RIGHT side of the image — a column occupying roughly the right 40% of the width, vertically centered, leaving the rest of the image clear"},
    {"key": "center", "label": "Center",
     "phrase": "the CENTER of the image — a centered column roughly 50–60% wide, balanced vertically, with the underlying subject still visible around it"},
    {"key": "top", "label": "Top",
     "phrase": "a band across the TOP of the image — spanning the upper ~40% of the height, leaving the lower portion clear"},
    {"key": "bottom", "label": "Bottom",
     "phrase": "a band across the BOTTOM of the image — spanning the lower ~40% of the height, leaving the upper portion clear"},
]

CTA_PLACEMENTS = [
    {"key": "bottom", "label": "Below text",
     "phrase": "directly below the sub-text with generous spacing, anchored at the bottom of the text block"},
    {"key": "left", "label": "Left",
     "phrase": "aligned to the LEFT, directly below the sub-text"},
    {"key": "center", "label": "Center",
     "phrase": "CENTERED horizontally, directly below the sub-text"},
    {"key": "right", "label": "Right",
     "phrase": "aligned to the RIGHT, directly below the sub-text"},
    {"key": "top", "label": "Above text",
     "phrase": "ABOVE the headline as a small pill, with the headline and sub-text beneath it"},
]

_TEXT_PLACEMENT_PHRASE = {p["key"]: p["phrase"] for p in TEXT_PLACEMENTS}
_CTA_PLACEMENT_PHRASE = {p["key"]: p["phrase"] for p in CTA_PLACEMENTS}


def text_placement_phrase(key: str) -> str:
    return _TEXT_PLACEMENT_PHRASE.get(key, _TEXT_PLACEMENT_PHRASE["left"])


def cta_placement_phrase(key: str) -> str:
    return _CTA_PLACEMENT_PHRASE.get(key, _CTA_PLACEMENT_PHRASE["bottom"])


# ── Aspect-ratio presets for the UI dropdown (spec §6.2) ──────────────────────
ASPECT_RATIO_PRESETS = [
    {
        "ar": ar,
        "label": meta["label"],
        "dimensions": f"{meta['w']}x{meta['h']}px",
        "w": meta["w"],
        "h": meta["h"],
        "orientation": meta["orientation"],
        "default": meta["default"],
    }
    for ar, meta in ASPECT_RATIOS.items()
]


def stage1_variant(variant_id: str) -> dict:
    return next(v for v in STAGE1_VARIANTS if v["id"] == variant_id.upper())


def stage2_variant(variant_id: str) -> dict:
    return next(v for v in STAGE2_VARIANTS if v["id"] == variant_id.upper())
