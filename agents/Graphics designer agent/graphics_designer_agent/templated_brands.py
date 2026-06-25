"""Templated brand packs (Phase C).

Builds a full ``BrandPack`` from a compact per-brand spec — palette, font and
domain copy — generating the Stage-1 gradients, colour system, brand-kit block
and suggestion content. The brand-neutral Stage-2 blend + Stage-4 composite
prompts are reused verbatim from Legal Soft (they only describe *how* to
composite, never the brand). Adding a brand is therefore data, not engine code.

Prompts are served inline (generated from the palette), so a templated brand
ships no ``.txt`` files — only its real fonts live on disk under ``brands/<id>/``.
These packs are intentionally a strong starting point you can refine later.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .prompts import PROMPT_DIR
from .registry import BrandPack

_BRANDS_DIR = Path(__file__).resolve().parent / "brands"

# Brand-neutral compositing prompts, reused by every templated brand.
STAGE2_BLEND_FILE = "stage2_element_blend.txt"
STAGE4_COMPOSITE_FILE = "stage4_logo_composite.txt"
_SHARED_BLEND = (PROMPT_DIR / STAGE2_BLEND_FILE).read_bytes().decode("utf-8")
_SHARED_COMPOSITE = (PROMPT_DIR / STAGE4_COMPOSITE_FILE).read_bytes().decode("utf-8")

# Gradient compositions. {L}/{M}/{D} = light / mid / deep brand hexes. Each opens
# with the literal "16:9 aspect ratio" anchor (tokens.STAGE1_AR_ANCHOR) so the AR
# substitution works, and ends "no noise, no text." like the canonical set.
_GRADIENT_TEMPLATES = [
    ("A", "Diagonal Sweep", "White top-left flowing into the deep brand tone bottom-right.",
     "linear-gradient(135deg, #FFFFFF 0%, {L} 35%, {M} 60%, {D} 100%)",
     "Create a 16:9 aspect ratio immersive abstract background gradient. Use a smooth diagonal "
     "sweep from the top-left to the bottom-right corner. Start with pure white #FFFFFF in the "
     "top-left, transitioning through soft {L} and {M} in the middle, and ending with deep {D} in "
     "the bottom-right. Soft, seamless blending with no harsh edges. Minimalist, cinematic, "
     "ultra-smooth gradient texture, high resolution, no noise, no text."),
    ("B", "Inverted Horizon", "Deep brand tone across the top dissolving to white at the bottom.",
     "linear-gradient(180deg, {D} 0%, {M} 55%, {L} 78%, #FFFFFF 100%)",
     "Create a 16:9 aspect ratio immersive abstract background gradient. A vertical wash with deep "
     "{D} across the top, melting through {M} and {L} toward pure white #FFFFFF at the bottom, each "
     "transition softly feathered so no band edge is visible. Soft, seamless blending with no harsh "
     "edges. Minimalist, cinematic, ultra-smooth gradient texture, high resolution, no noise, no text."),
    ("C", "Radial Bloom", "A soft white core blooming outward into the deep brand tone at the edges.",
     "radial-gradient(circle at 50% 45%, #FFFFFF 0%, {L} 35%, {M} 62%, {D} 100%)",
     "Create a 16:9 aspect ratio immersive abstract background gradient built as a centered radial "
     "bloom. Begin with a luminous pure white #FFFFFF core just above center, blooming outward "
     "through soft {L} and {M}, and settling into deep {D} at the outer edges with a gentle vignette. "
     "Soft, seamless blending with no harsh edges. Minimalist, cinematic, ultra-smooth gradient "
     "texture, high resolution, no noise, no text."),
    ("D", "Corner Spotlight", "A deep brand field lifted by a soft white spotlight from the upper-left.",
     "radial-gradient(ellipse at 20% 20%, #FFFFFF 0%, {L} 30%, {D} 82%)",
     "Create a 16:9 aspect ratio immersive abstract background gradient as a deep {D} field "
     "illuminated by a soft white #FFFFFF spotlight glowing from the upper-left corner, fading "
     "through {L} as it spreads toward the lower-right. Subtle, even falloff with a calm premium "
     "mood. Soft, seamless blending with no harsh edges. Minimalist, cinematic, ultra-smooth "
     "gradient texture, high resolution, no noise, no text."),
    ("E", "Dual-Origin Diagonal", "Two soft light sources meeting over a deep brand core.",
     "linear-gradient(135deg, #FFFFFF 0%, {M} 50%, {D} 100%)",
     "Create a 16:9 aspect ratio immersive abstract background gradient lit from two origins. A pure "
     "white #FFFFFF glow enters from the top-left and a soft {L} glow enters from the bottom-right, "
     "both dissolving over a {M} to deep {D} diagonal core where they meet. Balanced, airy and "
     "premium. Soft, seamless blending with no harsh edges. Minimalist, cinematic, ultra-smooth "
     "gradient texture, high resolution, no noise, no text."),
]

_GENERIC_QA = {
    1: "Check the gradient for banding in the upper third — regenerate if you see stepping.",
    2: "Verify hands and faces look natural and the subject blends cleanly into the background.",
    3: "Confirm the headline fits its column without clipping and the CTA pill reads cleanly.",
    4: "Confirm the logo sits flush with clean margins and nothing is cropped.",
}

_GENERIC_ONBOARDING = [
    {"id": "goal", "question": "What's the campaign goal?",
     "options": [{"id": "lead_gen", "label": "Lead generation"}, {"id": "brand", "label": "Brand awareness"}]},
    {"id": "audience", "question": "Who are we talking to?",
     "options": [{"id": "solo", "label": "Solo / small"}, {"id": "enterprise", "label": "Larger orgs"}]},
    {"id": "angle", "question": "What emotional angle?",
     "options": [{"id": "aspiration", "label": "Aspiration"}, {"id": "pain", "label": "Pain-point"}]},
]

# Brand-neutral discovery script (same option ids as suggestions.DISCOVERY_QUESTIONS,
# so the curated heuristics + direction synthesis work unchanged) with wording that
# doesn't assume a legal audience. Templated brands fall back to this.
_GENERIC_DISCOVERY = [
    {"id": "feeling", "group": "intent", "kind": "choice_text",
     "prompt": "First — what feeling or outcome should this creative land? "
               "Pick the closest, or tell me in your own words.",
     "options": [
         {"id": "trust", "label": "Trust & authority"},
         {"id": "urgency", "label": "Urgency / act now"},
         {"id": "warmth", "label": "Warmth & approachability"},
         {"id": "aspiration", "label": "Aspiration & growth"},
         {"id": "relief", "label": "Relief from overwhelm"},
     ],
     "placeholder": "e.g. calm confidence, “we’ve got your back”"},
    {"id": "audience", "group": "intent", "kind": "choice_text",
     "prompt": "Who are we talking to?",
     "options": [
         {"id": "solo", "label": "Small teams"},
         {"id": "partners", "label": "Leadership"},
         {"id": "inhouse", "label": "In-house teams"},
         {"id": "growing", "label": "Growing orgs"},
     ],
     "placeholder": "Describe the audience"},
    {"id": "tone", "group": "intent", "kind": "choice",
     "prompt": "What tone should it strike?",
     "options": [
         {"id": "premium", "label": "Premium & polished"},
         {"id": "bold", "label": "Bold & punchy"},
         {"id": "friendly", "label": "Friendly & human"},
         {"id": "formal", "label": "Formal & institutional"},
     ]},
    {"id": "style", "group": "intent", "kind": "choice_text", "optional": True,
     "prompt": "Any visual style you’re leaning toward? (optional)",
     "options": [
         {"id": "minimal", "label": "Minimal & clean"},
         {"id": "editorial", "label": "Editorial"},
         {"id": "cinematic", "label": "Cinematic"},
         {"id": "corporate", "label": "Corporate"},
     ],
     "placeholder": "Optional — references or styling notes"},
    {"id": "event", "group": "context", "kind": "choice_text",
     "prompt": "Is this for a specific event or moment, or is it evergreen?",
     "options": [
         {"id": "evergreen", "label": "Evergreen"},
         {"id": "webinar", "label": "Webinar / event"},
         {"id": "hiring", "label": "Hiring push"},
         {"id": "seasonal", "label": "Seasonal / holiday"},
         {"id": "launch", "label": "Launch / announcement"},
     ],
     "placeholder": "Name the event, date or moment"},
    {"id": "theme", "group": "context", "kind": "text", "optional": True,
     "prompt": "Last one — anything specific in mind for the theme or message? (optional)",
     "placeholder": "e.g. a tagline, a campaign name, a seasonal angle"},
]


def _gradient_artifacts(palette: dict):
    """Return (stage1_variants, inline_prompt_map, curated_gradients) for a palette."""
    L, M, D = palette["light"], palette["mid"], palette["deep"]
    variants, inline, curated = [], {}, []
    for vid, title, desc, css, prompt in _GRADIENT_TEMPLATES:
        text = prompt.format(L=L, M=M, D=D)
        css_filled = css.format(L=L, M=M, D=D)
        fname = f"stage1_gradient_{vid}.txt"
        variants.append({"id": vid, "prompt_file": fname, "title": title,
                         "desc": desc, "css_gradient": css_filled})
        inline[fname] = text
        curated.append({"cid": f"grad-{vid.lower()}", "title": title, "desc": desc,
                        "css_gradient": css_filled, "prompt": text})
    return variants, inline, curated


def _locked_colors(palette: dict) -> dict:
    return {
        "gradient": ["#FFFFFF", palette["light"], palette["mid"], palette["deep"]],
        "text": palette["ink"],
        "accent": palette["accent"],
        "headline_highlight": {"from": palette["hl_from"], "to": palette["hl_to"],
                               "direction": "left-to-right linear"},
        "cta": {"from": palette["cta_from"], "to": palette["cta_to"],
                "direction": "135° diagonal",
                "shadow": "rgba(0, 0, 0, 0.22) 0 8px 20px"},
    }


def _text_colors(palette: dict) -> list:
    return [
        {"key": "dark", "label": "Dark", "swatch": palette["ink"],
         "phrase": f"solid near-black {palette['ink']}"},
        {"key": "gradient", "label": "Brand gradient",
         "swatch": f"linear-gradient(90deg, {palette['hl_from']}, {palette['hl_to']})",
         "phrase": f"a smooth left-to-right linear gradient from {palette['hl_from']} to "
                   f"{palette['hl_to']} applied across the glyphs"},
        {"key": "white", "label": "White", "swatch": "#FFFFFF", "phrase": "solid white #FFFFFF"},
    ]


def _brand_kit_block(name: str, palette: dict) -> str:
    return (
        "{\n"
        f"Brand: {name}\n"
        f"Primary: {palette['mid']}\n"
        f"Deep: {palette['deep']}\n"
        f"Light: {palette['light']}\n"
        f"Accent: {palette['accent']}\n"
        f"Ink: {palette['ink']}\n"
        "}"
    )


def build_templated_pack(spec: dict) -> BrandPack:
    palette = spec["palette"]
    stage1_variants, grad_inline, curated_gradients = _gradient_artifacts(palette)
    inline_prompts = {
        **grad_inline,
        STAGE2_BLEND_FILE: _SHARED_BLEND,
        STAGE4_COMPOSITE_FILE: _SHARED_COMPOSITE,
    }
    canonical = {name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                 for name, text in inline_prompts.items()}
    hexes = {h.upper() for h in (
        "#FFFFFF", palette["light"], palette["mid"], palette["deep"], palette["accent"],
        palette["hl_from"], palette["hl_to"])}

    elements = spec["stage2_variants"]
    return BrandPack(
        id=spec["id"],
        name=spec["name"],
        prompts_dir=PROMPT_DIR,            # unused (inline_prompts wins) — kept non-None
        fonts_dir=_BRANDS_DIR / spec["id"] / "fonts",
        canonical_sha256=canonical,
        firestore_brand_id=spec.get("firestore_brand_id"),
        inline_prompts=inline_prompts,
        locked_colors=_locked_colors(palette),
        brand_kit_block=_brand_kit_block(spec["name"], palette),
        source_note_stage1=spec.get(
            "source_note",
            "Strict: use only the brand palette below. Backgrounds are blue/neutral with the "
            "brand accent — never introduce off-brand colours."),
        text_colors=_text_colors(palette),
        stage1_variants=stage1_variants,
        stage2_variants=elements,
        stage2_blend_prompt=STAGE2_BLEND_FILE,
        stage2_categories=spec.get("stage2_categories",
                                   ["people", "object", "flatlay", "architecture", "scene"]),
        font_family=spec["font_family"],
        font_variants=spec["font_variants"],
        default_font=spec["default_font"],
        default_headline=spec["default_headline"],
        default_highlight=spec["default_highlight"],
        default_subtext_1=spec["default_subtext_1"],
        default_subtext_2=spec["default_subtext_2"],
        default_cta=spec["default_cta"],
        onboarding_questions=spec.get("onboarding_questions", _GENERIC_ONBOARDING),
        discovery_questions=spec.get("discovery_questions", _GENERIC_DISCOVERY),
        concept_rationale={e["id"]: e["desc"] for e in elements},
        hooks=spec["hooks"],
        ctas=spec["ctas"],
        qa=spec.get("qa", _GENERIC_QA),
        explore_reason={e["id"]: e["desc"] for e in elements},
        explore_order=[e["id"] for e in elements],
        curated_gradients=curated_gradients,
        curated_elements=spec.get("curated_elements", [])
        or [{"cid": e["id"].lower(), "title": e["title"], "desc": e["desc"],
             "category": e["category"], "subject": e["subject"]} for e in elements[:4]],
        brand_gradient_hexes=hexes,
    )


# Be Vietnam family (MedVirtual) — every weight + italics present in the kit.
_BEVIETNAM_FULL = [
    {"name": "Be Vietnam Thin", "weight": 100, "style": "normal", "file": "BeVietnam-Thin.ttf"},
    {"name": "Be Vietnam Thin Italic", "weight": 100, "style": "oblique", "file": "BeVietnam-ThinItalic.ttf"},
    {"name": "Be Vietnam Light", "weight": 300, "style": "normal", "file": "BeVietnam-Light.ttf"},
    {"name": "Be Vietnam Light Italic", "weight": 300, "style": "oblique", "file": "BeVietnam-LightItalic.ttf"},
    {"name": "Be Vietnam Regular", "weight": 400, "style": "normal", "file": "BeVietnam-Regular.ttf"},
    {"name": "Be Vietnam Italic", "weight": 400, "style": "oblique", "file": "BeVietnam-Italic.ttf"},
    {"name": "Be Vietnam Medium", "weight": 500, "style": "normal", "file": "BeVietnam-Medium.ttf"},
    {"name": "Be Vietnam Medium Italic", "weight": 500, "style": "oblique", "file": "BeVietnam-MediumItalic.ttf"},
    {"name": "Be Vietnam SemiBold", "weight": 600, "style": "normal", "file": "BeVietnam-SemiBold.ttf"},
    {"name": "Be Vietnam SemiBold Italic", "weight": 600, "style": "oblique", "file": "BeVietnam-SemiBoldItalic.ttf"},
    {"name": "Be Vietnam Bold", "weight": 700, "style": "normal", "file": "BeVietnam-Bold.ttf"},
    {"name": "Be Vietnam Bold Italic", "weight": 700, "style": "oblique", "file": "BeVietnam-BoldItalic.ttf"},
    {"name": "Be Vietnam ExtraBold", "weight": 800, "style": "normal", "file": "BeVietnam-ExtraBold.ttf"},
    {"name": "Be Vietnam ExtraBold Italic", "weight": 800, "style": "oblique", "file": "BeVietnam-ExtraBoldItalic.ttf"},
]

# Remote Attorneys kit ships only these two faces.
_REMOTE_FONTS = [
    {"name": "Be Vietnam Bold", "weight": 700, "style": "normal", "file": "BeVietnam-Bold.ttf"},
    {"name": "SF UI Text Regular", "weight": 400, "style": "normal", "file": "SFUIText-Regular.ttf"},
]


_MEDVIRTUAL = {
    "id": "medvirtual",
    "name": "MedVirtual",
    "firestore_brand_id": "7a068f2810ae4b32b338b9037e6e3fb4",
    "palette": {
        "light": "#A1D7E2", "mid": "#24B9CE", "deep": "#137A9A", "accent": "#19B1E3",
        "ink": "#161511", "hl_from": "#19B1E3", "hl_to": "#137A9A",
        "cta_from": "#24B9CE", "cta_to": "#137A9A",
    },
    "font_family": "Be Vietnam",
    "font_variants": _BEVIETNAM_FULL,
    "default_font": "Be Vietnam Bold",
    "default_headline": "Hire Vetted Medical Virtual Assistants",
    "default_highlight": "Medical Virtual Assistants",
    "default_subtext_1": "Trained healthcare VAs, ready when you are.",
    "default_subtext_2": "Onboard HIPAA-aware talent in days, not months.",
    "default_cta": "Book a Free Consultation",
    "stage2_variants": [
        {"id": "A", "title": "Medical VA at Desk", "angle": "efficiency / warmth", "category": "people",
         "desc": "Warm medical VA with a headset; lower frame, upper area open.",
         "subject": "A warm, professional medical virtual assistant in smart business-casual attire wearing a slim headset, seated at a tidy desk looking slightly off-camera as if on a patient call, cinematic shallow depth of field. She occupies the lower portion of the frame; keep the upper area open."},
        {"id": "B", "title": "Confident Male VA", "angle": "authority", "category": "people",
         "desc": "Composed male medical VA on the right; left open.",
         "subject": "A confident male medical virtual assistant in neat business-casual attire with a slim headset, calm assured posture, occupying the right side of the frame and leaving the left open."},
        {"id": "C", "title": "Minimal Stethoscope", "angle": "simplicity / care", "category": "object",
         "desc": "Ultra-minimal 3D stethoscope; centered-low, open frame.",
         "subject": "An ultra-minimal matte 3D rendering of a stethoscope resting in a gentle coil with one soft shadow beneath, centered-low with vast empty space around it. Restrained and premium, no clutter."},
        {"id": "D", "title": "Clinic Desk Flatlay", "angle": "organised productivity", "category": "flatlay",
         "desc": "Top-down tidy medical desk; cluster lower-right.",
         "subject": "A top-down flatlay of a tidy medical-office desk: an open silver laptop, a clipboard with a blank chart, a stethoscope, folded glasses and a small white coffee cup, arranged in the lower-right cluster with the upper-left kept clear. Soft overhead daylight, faint natural shadows, clean isolated objects."},
        {"id": "E", "title": "Modern Clinic Tower", "angle": "scale / trust", "category": "architecture",
         "desc": "Clean clinic building on the right; left open.",
         "subject": "A modern medical clinic building with a clean glass-and-stone facade shot from a gentle upward angle at golden hour, occupying the right side of the frame and leaving the left open. No people, no signage text."},
        {"id": "F", "title": "Quiet Clinic, After Hours", "angle": "pain-point", "category": "scene",
         "desc": "Empty calm clinic reception in the evening — right side.",
         "subject": "An empty calm clinic reception after hours — a tidy desk, a vacant ergonomic chair and soft ambient light, NO people — evoking a quiet end-of-day moment, set into the right side of the frame with ample negative space."},
    ],
    "hooks": {
        "A": {
            "headlines": [
                ("Hire Vetted Medical Virtual Assistants", "Medical Virtual Assistants"),
                ("Scale Your Practice With Medical VAs", "Medical VAs"),
                ("Add Trusted Medical Virtual Staff Today", "Medical Virtual Staff"),
                ("Grow Faster With Medical Virtual Assistants", "Medical Virtual Assistants"),
                ("Reclaim Your Time With Medical VAs", "Medical VAs"),
            ],
            "subtext": [
                ("Trained healthcare VAs, ready when you are.", "Onboard HIPAA-aware talent in days."),
                ("Stop drowning in admin and charting.", "Delegate the overflow to vetted medical VAs."),
            ],
        },
    },
    "ctas": ["Book a Free Consultation", "Get Started Today", "Hire Your Medical VA"],
}


_REMOTE_ATTORNEYS = {
    "id": "remote_attorneys",
    "name": "Remote Attorneys",
    "firestore_brand_id": "615ef2acaf1f4438beb29434652e1633",
    "palette": {
        "light": "#FFAF50", "mid": "#EF8200", "deep": "#B15714", "accent": "#FF9132",
        "ink": "#0F0F0F", "hl_from": "#FF9132", "hl_to": "#B15714",
        "cta_from": "#EF8200", "cta_to": "#B15714",
    },
    "font_family": "Be Vietnam",
    "font_variants": _REMOTE_FONTS,
    "default_font": "Be Vietnam Bold",
    "default_headline": "Hire Experienced Remote Attorneys",
    "default_highlight": "Remote Attorneys",
    "default_subtext_1": "Build your legal team with vetted talent.",
    "default_subtext_2": "Start with pre-vetted attorneys in days.",
    "default_cta": "Book a Free Consultation",
    "stage2_variants": [
        {"id": "A", "title": "Remote Attorney at Desk", "angle": "authority", "category": "people",
         "desc": "Confident remote attorney with a headset; lower frame, upper open.",
         "subject": "A confident remote attorney in a tailored blazer with a slim headset, seated at a tidy desk looking slightly off-camera as if on a client call, cinematic shallow depth of field. Subject in the lower portion of the frame; keep the upper area open."},
        {"id": "B", "title": "Poised Paralegal", "angle": "efficiency / warmth", "category": "people",
         "desc": "Composed paralegal with a headset; lower-right, neg space upper-left.",
         "subject": "A poised paralegal in smart professional attire with a slim headset, calm assured expression, occupying the lower-right with generous negative space in the upper-left."},
        {"id": "C", "title": "Scales of Justice", "angle": "trust / fairness", "category": "object",
         "desc": "Ultra-minimal 3D balanced scales; centered-low, open frame.",
         "subject": "An ultra-minimal matte 3D rendering of a balanced set of scales of justice, perfectly level, with one soft shadow beneath, centered-low with vast empty space around it. Restrained, premium, no clutter."},
        {"id": "D", "title": "Gavel at Rest", "angle": "authority", "category": "object",
         "desc": "Minimal 3D gavel on its block; lower-center, open frame.",
         "subject": "An ultra-minimal 3D rendering of a single wooden gavel resting on its sound block at a slight angle with one soft shadow, muted tones, set in the lower-center with the frame mostly open. No clutter."},
        {"id": "E", "title": "Signed Contract Flatlay", "angle": "deal closed / trust", "category": "flatlay",
         "desc": "Top-down signed document, pen on the line; cluster lower-right.",
         "subject": "A top-down flatlay of a signed legal document with a fountain pen resting on the signature line, a pair of folded glasses and a white coffee cup, clustered in the lower-right with the upper-left kept clear. Soft overhead daylight, faint natural shadows, sharp isolated objects."},
        {"id": "F", "title": "Law Office Tower", "angle": "prestige / authority", "category": "architecture",
         "desc": "Prestigious legal tower on the right; left open.",
         "subject": "A single prestigious law-office tower with modern granite-and-glass architecture shot from a heroic upward angle at golden hour, occupying the right side of the frame and leaving the left open. No people, no signage text."},
    ],
    "hooks": {
        "A": {
            "headlines": [
                ("Hire Experienced Remote Attorneys", "Remote Attorneys"),
                ("Scale Your Firm With Remote Attorneys", "Remote Attorneys"),
                ("Add Trusted Remote Legal Staff Today", "Remote Legal Staff"),
                ("Grow Faster With Remote Attorneys", "Remote Attorneys"),
                ("Reclaim Your Evenings With Remote Attorneys", "Remote Attorneys"),
            ],
            "subtext": [
                ("Build your legal team with vetted talent.", "Start with pre-vetted attorneys in days."),
                ("Your caseload shouldn't cost your nights.", "Delegate the overflow to remote attorneys."),
            ],
        },
    },
    "ctas": ["Book a Free Consultation", "Get Started Today", "Hire Your Attorney"],
    "onboarding_questions": [
        {"id": "goal", "question": "What's the campaign goal?",
         "options": [{"id": "lead_gen", "label": "Lead generation"}, {"id": "brand", "label": "Brand awareness"}]},
        {"id": "audience", "question": "Who are we talking to?",
         "options": [{"id": "solo", "label": "Solo attorneys"}, {"id": "partners", "label": "Firm partners"}]},
        {"id": "angle", "question": "What emotional angle?",
         "options": [{"id": "aspiration", "label": "Aspiration"}, {"id": "pain", "label": "Pain-point"}]},
    ],
}


SPECS = [_MEDVIRTUAL, _REMOTE_ATTORNEYS]


def build_all() -> list[BrandPack]:
    return [build_templated_pack(s) for s in SPECS]
