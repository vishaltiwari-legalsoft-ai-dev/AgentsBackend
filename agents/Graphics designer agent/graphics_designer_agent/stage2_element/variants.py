"""Stage-2 UI metadata: the subject/element library.

Stage 2 adds ONE subject to the approved Stage-1 background through the single
immutable blend prompt (``../prompts/stage2_element_blend.txt``). Each variant
supplies only the ``subject`` text; ``title``/``desc`` are UI copy and
``category`` groups the picker + the agent explorer.
"""

from __future__ import annotations

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

# Stage-2 subject placement (prompt-steered, spec §5.2b). "auto" preserves each
# subject's built-in framing (a strict no-op); the 9 cells inject an explicit
# override clause. Grid keys mirror stage4_logo.options.LOGO_POSITIONS so the
# picker reuses the same 3×3 component; "auto" is the leading default chip.
STAGE2_PLACEMENTS = [
    {"key": "auto", "label": "Auto", "row": -1, "col": -1},
    {"key": "top-left", "label": "Top left", "row": 0, "col": 0},
    {"key": "top-center", "label": "Top", "row": 0, "col": 1},
    {"key": "top-right", "label": "Top right", "row": 0, "col": 2},
    {"key": "middle-left", "label": "Left", "row": 1, "col": 0},
    {"key": "middle-center", "label": "Middle", "row": 1, "col": 1},
    {"key": "middle-right", "label": "Right", "row": 1, "col": 2},
    {"key": "bottom-left", "label": "Bottom left", "row": 2, "col": 0},
    {"key": "bottom-center", "label": "Bottom", "row": 2, "col": 1},
    {"key": "bottom-right", "label": "Bottom right", "row": 2, "col": 2},
]
