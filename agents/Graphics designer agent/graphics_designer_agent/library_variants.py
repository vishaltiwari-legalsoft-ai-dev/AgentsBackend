"""Design-library variants — Stage-1/Stage-2 options distilled from the brands'
real, human-designed creatives (the ``prompt-library/`` analysis of
``LS DESIGN PRODUCTIONS``).

Every entry here reverse-engineers a recurring background system or subject
treatment observed across a brand's shipped ads and collateral, so the studio
can offer "what the human designer used to make" as first-class Step-1 /
Step-2 picks.

Append-only by design: ``extend_pack`` returns a copy of a ``BrandPack`` with
these variants appended AFTER the brand's canonical set (never shadowing an
existing id) and the Stage-1 prompt texts served inline. Canonical ``.txt``
prompts, their frozen hashes and every existing variant stay byte-identical —
a brand with no library entry passes through untouched.

Prompt contract (same as every Stage-1 prompt): the text opens with the
literal "16:9 aspect ratio" anchor (see ``stage1_gradient.prompting``) and
describes a background only — subjects belong to Stage 2, text to Stage 3.
"""

from __future__ import annotations

import dataclasses

# ── Legal Soft — from Ad Creatives / Advertising batches / collateral ─────────
# Palette: navy #1746A2, royal #2653AB, periwinkle #85AEFD/#BDCFED, icy
# #EAF1FC; orange accent #FF8A3D → #F26A1A. (prompt-library/legal-soft/)

_LEGALSOFT_STAGE1 = [
    {
        "id": "M",
        "title": "Halftone Horizon",
        "desc": "From past creatives — royal blue field with dotted halftone arcs and a faint chart watermark.",
        "css_gradient": "linear-gradient(180deg, #2653AB 0%, #1746A2 70%, #0E2A5E 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio premium advertising background. A rich royal blue field "
            "graduating from #2653AB at the top into deep navy #1746A2 toward the bottom. Along the "
            "lower third, add a subtle horizon made of small periwinkle #85AEFD halftone dots that "
            "shrink and fade as they rise, plus one faint, barely-visible upward bar-chart-arrow "
            "watermark ghosted at about 8% opacity in the lower-right. Clean, corporate, modern SaaS "
            "feel. Soft, seamless blending with no harsh edges. Minimalist, cinematic, ultra-smooth "
            "texture, high resolution, no noise, no text."
        ),
    },
    {
        "id": "N",
        "title": "Blue-Orange Light Streaks",
        "desc": "From past creatives — abstract blurred blue and orange light-streak gradient.",
        "css_gradient": "linear-gradient(120deg, #1746A2 0%, #2653AB 45%, #F26A1A 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio immersive abstract background of soft, heavily blurred light "
            "streaks. Deep royal blue #1746A2 and #2653AB ribbons of light sweep diagonally from the "
            "left, meeting warm orange #FF8A3D to #F26A1A glow entering from the right edge, the two "
            "families melting into each other like out-of-focus long-exposure trails. Dark enough at "
            "the edges for white text to sit on. Soft, seamless blending with no harsh edges. "
            "Cinematic, ultra-smooth bokeh texture, high resolution, no noise, no text."
        ),
    },
    {
        "id": "O",
        "title": "Icy Studio White",
        "desc": "From past creatives — icy near-white canvas with faint periwinkle wireframe mesh textures.",
        "css_gradient": "linear-gradient(180deg, #FFFFFF 0%, #EAF1FC 60%, #BDCFED 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio light studio advertising background. An airy icy canvas "
            "flowing from pure white #FFFFFF at the top into pale ice blue #EAF1FC and soft "
            "periwinkle #BDCFED at the bottom. Decorate the corners with faint, thin-line periwinkle "
            "#85AEFD wireframe mesh textures — a partial mesh globe in one upper corner and gentle "
            "concentric line traces in the opposite lower corner — all at low opacity so the center "
            "stays clean and open. Soft, seamless blending with no harsh edges. Minimalist, premium, "
            "ultra-smooth, high resolution, no noise, no text."
        ),
    },
    {
        "id": "P",
        "title": "Deep Navy Dot Field",
        "desc": "From past creatives — dark navy compliance-ad field with a subtle polka-dot texture.",
        "css_gradient": "radial-gradient(ellipse at 30% 30%, #2653AB 0%, #1746A2 55%, #0E2A5E 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio dark premium advertising background. A deep navy field "
            "graduating from #1746A2 into an even darker #0E2A5E toward the edges with a soft "
            "#2653AB lift in the upper-left. Scatter a sparse, subtle polka-dot texture of tiny "
            "periwinkle #85AEFD dots at roughly 10% opacity across one diagonal band, fading out "
            "before the center. Serious, authoritative mood suited to a warning or compliance "
            "message. Soft, seamless blending with no harsh edges. Minimalist, cinematic, "
            "ultra-smooth, high resolution, no noise, no text."
        ),
    },
    {
        "id": "Q",
        "title": "Orange Panel on Blue",
        "desc": "From past creatives — royal blue field with a rounded orange gradient offer panel.",
        "css_gradient": "linear-gradient(90deg, #1746A2 0%, #1746A2 55%, #FF8A3D 75%, #F26A1A 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio premium advertising background. A solid royal blue #1746A2 "
            "field occupying the frame, with one large, softly rounded rectangular panel filled with "
            "a warm orange gradient from #FF8A3D to #F26A1A anchored along the right third and "
            "bleeding off the right edge. The panel edge is crisp but its lighting is soft, with a "
            "gentle shadow lifting it slightly off the blue. Keep the blue area smooth and open for "
            "copy. Soft, seamless blending elsewhere with no harsh edges. Bold, modern, high "
            "resolution, no noise, no text."
        ),
    },
    {
        "id": "R",
        "title": "Periwinkle Headline Wash",
        "desc": "From past creatives — luminous periwinkle-to-royal sweep used behind two-tone headlines.",
        "css_gradient": "linear-gradient(135deg, #85AEFD 0%, #2653AB 60%, #1746A2 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio immersive abstract background gradient. A luminous diagonal "
            "sweep starting with bright periwinkle #85AEFD in the upper-left, deepening through "
            "royal #2653AB and settling into navy #1746A2 in the lower-right, with one soft "
            "white-ish glow feathered into the top edge. Rich enough for white text yet vivid and "
            "optimistic. Soft, seamless blending with no harsh edges. Minimalist, cinematic, "
            "ultra-smooth gradient texture, high resolution, no noise, no text."
        ),
    },
]

_LEGALSOFT_STAGE2 = [
    {
        "id": "T",
        "title": "Headset Pro Cut-out",
        "desc": "From past creatives — studio-lit professional with a slim black headset, flush right.",
        "angle": "signature brand look",
        "category": "people",
        "subject": (
            "A photorealistic, studio-lit professional virtual assistant in smart business attire "
            "wearing a slim black headset, smiling gently with direct, confident eye contact, "
            "cleanly cut out with crisp edges and cropped by the right edge of the canvas so she "
            "occupies the right 40% of the frame; warm key light with a cool fill, premium "
            "advertising finish, the left side left completely open."
        ),
    },
    {
        "id": "U",
        "title": "Laptop Video Call",
        "desc": "From past creatives — open laptop showing a five-tile video-conference grid.",
        "angle": "remote collaboration",
        "category": "object",
        "subject": (
            "A photorealistic open silver laptop on a clean desk surface, its screen showing a "
            "video-conference grid of five professional participants in business attire — one large "
            "speaker tile and four smaller tiles — with all interface text blurred beyond "
            "readability. Placed in the lower-right of the frame at a slight three-quarter angle, "
            "soft screen glow, shallow depth of field, upper-left kept open."
        ),
    },
    {
        "id": "V",
        "title": "Piggy Bank Savings",
        "desc": "From past creatives — a hand presenting a teal piggy bank; savings story.",
        "angle": "cost savings",
        "category": "object",
        "subject": (
            "A photorealistic close-up of a professional's hand in a navy suit sleeve presenting a "
            "glossy teal ceramic piggy bank on an open palm, one soft coin glint on the slot, "
            "studio-lit against soft shadow, positioned in the lower-right third with the rest of "
            "the frame open — a clean visual metaphor for staffing-cost savings."
        ),
    },
    {
        "id": "W",
        "title": "Floating Dashboard Cards",
        "desc": "From past creatives — white staffing-dashboard UI cards floating at a slight tilt.",
        "angle": "product / platform",
        "category": "object",
        "subject": (
            "Two clean white rounded-rectangle dashboard UI cards floating at a slight tilt with "
            "soft drop shadows — one suggesting a staff-profile card with a small circular avatar "
            "photo and soft grey placeholder bars, one suggesting a stats card with a simple blue "
            "bar chart — every label an unreadable soft grey bar, no legible text. Clustered in the "
            "right third of the frame, gentle perspective, premium SaaS product feel, left side open."
        ),
    },
    {
        "id": "X",
        "title": "Headset Duo",
        "desc": "From past creatives — two professionals with headsets working side by side.",
        "angle": "team scale",
        "category": "people",
        "subject": (
            "A photorealistic pair of professional virtual assistants — a woman in a blazer and a "
            "man in a crisp shirt, both wearing slim black headsets — seated side by side and "
            "focused on their screens as if handling client calls, warm candid studio lighting, "
            "cleanly composited along the bottom-right and cropped by the frame edge, the upper-left "
            "half left open for copy."
        ),
    },
    {
        "id": "Y",
        "title": "Cinematic Gavel",
        "desc": "From past creatives — dark cinematic wooden gavel scene, moody rim light.",
        "angle": "legal gravitas",
        "category": "scene",
        "subject": (
            "A cinematic photorealistic wooden gavel resting on its sound block on a dark reflective "
            "desk, dramatic low-key lighting with one cool rim light tracing the gavel head and a "
            "soft warm glow behind, shallow depth of field, set into the lower-right of the frame "
            "with generous dark open space above for a headline."
        ),
    },
]

# ── MedVirtual — from Blog Graphics / Newsletter Graphics ─────────────────────
# Palette: light #A1D7E2, mid #24B9CE, deep #137A9A, accent #19B1E3.
# (prompt-library/medvirtual/) — templated-brand tests require the deep hex in
# every Stage-1 prompt.

_MEDVIRTUAL_STAGE1 = [
    {
        "id": "F",
        "title": "Diagonal Teal Wash",
        "desc": "From past creatives — the blog-header wash: deep teal sweeping in from the lower-left.",
        "css_gradient": "linear-gradient(45deg, #137A9A 0%, #24B9CE 45%, #A1D7E2 75%, #FFFFFF 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio premium background wash. A diagonal teal gradient enters "
            "from the bottom-left corner — dense deep teal #137A9A melting through #24B9CE and "
            "light aqua #A1D7E2 as it sweeps up toward a soft near-white glow in the top-right, the "
            "wash thinning to transparency past the midline so the upper-right stays airy and "
            "bright. Calm, clinical, trustworthy healthcare mood. Soft, seamless blending with no "
            "harsh edges. Minimalist, ultra-smooth, high resolution, no noise, no text."
        ),
    },
    {
        "id": "G",
        "title": "Contour Arc Banner",
        "desc": "From past creatives — teal gradient with thin translucent white contour-line arcs.",
        "css_gradient": "linear-gradient(90deg, #137A9A 0%, #24B9CE 60%, #19B1E3 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio premium banner background. A horizontal teal gradient from "
            "deep #137A9A on the left through #24B9CE into bright cyan #19B1E3 on the right, "
            "overlaid with a few thin, translucent white contour-line arcs — elegant concentric "
            "curves at about 15% opacity flowing across the lower half like gentle topographic "
            "lines. Clean, modern healthcare-newsletter feel. Soft, seamless blending with no harsh "
            "edges. Minimalist, ultra-smooth, high resolution, no noise, no text."
        ),
    },
    {
        "id": "H",
        "title": "Clinical Off-White Canvas",
        "desc": "From past creatives — the infographic canvas: off-white with pale oversized brand shapes.",
        "css_gradient": "linear-gradient(180deg, #FFFFFF 0%, #F4FAFC 55%, #A1D7E2 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio light infographic background. A clean off-white canvas with "
            "a whisper of aqua #A1D7E2 warming the bottom edge, decorated with barely-visible, "
            "oversized brand shapes — one huge pale #24B9CE circle bleeding off the top-right and a "
            "soft rounded plus/cross shape ghosted near the lower-left, both at roughly 6% opacity "
            "over the white, with the deepest tone #137A9A only hinted inside the shapes. Ample "
            "clear space across the middle. Soft, seamless blending with no harsh edges. Minimalist, "
            "clinical, ultra-smooth, high resolution, no noise, no text."
        ),
    },
]

_MEDVIRTUAL_STAGE2 = [
    {
        "id": "G",
        "title": "Teal-Scrubs VA",
        "desc": "From past creatives — VA in teal scrubs with a headset, teal-graded, right side.",
        "angle": "signature brand look",
        "category": "people",
        "subject": (
            "A warm, photorealistic medical virtual assistant in teal scrubs wearing a slim headset, "
            "smiling softly mid-call, shot with shallow depth of field and color-graded gently "
            "toward the teal family, occupying the right side of the frame and cropped by the right "
            "edge, the left side left completely open."
        ),
    },
    {
        "id": "H",
        "title": "Typing Hands Macro",
        "desc": "From past creatives — macro of hands typing on a softly backlit keyboard.",
        "angle": "back-office productivity",
        "category": "object",
        "subject": (
            "A photorealistic macro close-up of two hands typing on a softly backlit laptop "
            "keyboard, cool teal-tinted screen glow on the fingers, shallow depth of field melting "
            "the background away, composed along the lower band of the frame with airy open space "
            "above."
        ),
    },
    {
        "id": "I",
        "title": "Doctor with Smartphone",
        "desc": "From past creatives — white-coat torso scrolling a smartphone; no face shown.",
        "angle": "modern practice",
        "category": "people",
        "subject": (
            "A photorealistic mid-torso shot of a doctor in a crisp white coat with a stethoscope "
            "around the neck, holding and scrolling a smartphone with both hands — face out of "
            "frame — soft bright clinic light, gentle teal grade, positioned in the right third "
            "with the left two-thirds open."
        ),
    },
]

# ── Remote Attorneys — from Advertising batches ───────────────────────────────
# Palette: light #FFAF50, mid #EF8200, deep #B15714, accent #FF9132.
# (prompt-library/remote-attorneys/) — deep hex required in Stage-1 prompts.

_REMOTE_ATTORNEYS_STAGE1 = [
    {
        "id": "F",
        "title": "Orange Swoosh Field",
        "desc": "From past creatives — light gray field with a giant flat-orange swoosh ribbon.",
        "css_gradient": "linear-gradient(135deg, #F0F0F0 0%, #F0F0F0 55%, #EF8200 80%, #B15714 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio premium advertising background. A flat, even light-gray "
            "#F0F0F0 studio field crossed by one giant, flat orange swoosh — a smooth ribbon shape "
            "in #EF8200 sweeping in from the lower-right corner, curling toward the center and "
            "deepening to #B15714 at its trailing edge, matte with a crisp silhouette and no "
            "gradient banding. The upper-left half stays clean and open. Bold, graphic, modern. "
            "High resolution, no noise, no text."
        ),
    },
    {
        "id": "G",
        "title": "Full-Bleed Orange",
        "desc": "From past creatives — confident flat orange field with a soft deep-orange vignette.",
        "css_gradient": "radial-gradient(ellipse at 50% 40%, #FF9132 0%, #EF8200 60%, #B15714 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio bold advertising background. A confident full-bleed orange "
            "field — luminous #FF9132 at the upper-center easing into brand orange #EF8200 across "
            "the body and settling into a soft deep #B15714 vignette at the corners and bottom "
            "edge. Rich enough for pure white text anywhere. Soft, seamless blending with no harsh "
            "edges. Minimalist, ultra-smooth, high resolution, no noise, no text."
        ),
    },
    {
        "id": "H",
        "title": "Warm Peach Grid",
        "desc": "From past creatives — cream-peach canvas with a faint fine grid pattern.",
        "css_gradient": "linear-gradient(180deg, #FDF3E7 0%, #FAE3C8 70%, #FFAF50 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio light editorial background. A warm cream-peach canvas "
            "flowing from soft ivory at the top into a gentle #FFAF50 warmth at the bottom edge, "
            "overlaid with a faint fine grid of thin lines at about 8% opacity — a subtle "
            "blueprint-like texture — and one soft #EF8200 glow feathered into the lower-right "
            "corner with a hint of deep #B15714 at the very edge. Calm, premium, spacious. Soft, "
            "seamless blending with no harsh edges. High resolution, no noise, no text."
        ),
    },
]

_REMOTE_ATTORNEYS_STAGE2 = [
    {
        "id": "G",
        "title": "Blazered Attorney Cut-out",
        "desc": "From past creatives — chest-up smiling attorney in a tailored blazer, clean edges.",
        "angle": "signature brand look",
        "category": "people",
        "subject": (
            "A photorealistic chest-up studio portrait of a confident, warmly smiling attorney in a "
            "tailored charcoal blazer over a crisp shirt, soft even lighting with a warm grade, "
            "cleanly cut out with crisp edges and cropped by the right edge of the canvas so the "
            "subject fills the right third, the left side left completely open."
        ),
    },
    {
        "id": "H",
        "title": "Lady Justice Statue",
        "desc": "From past creatives — gold Lady Justice statue, editorial crop.",
        "angle": "legal authority",
        "category": "object",
        "subject": (
            "A photorealistic golden Lady Justice statue holding balanced scales, shot in an "
            "editorial close crop from a slight low angle with soft warm studio light glinting off "
            "the gold, placed along the right edge of the frame and partially bleeding off it, "
            "generous open space to the left."
        ),
    },
    {
        "id": "I",
        "title": "Dual Attorney Portraits",
        "desc": "From past creatives — two female attorneys side by side, offer-ad energy.",
        "angle": "choice / talent depth",
        "category": "people",
        "subject": (
            "Two photorealistic studio portraits of professional female attorneys in tailored "
            "blazers standing side by side with confident, friendly expressions, evenly lit with a "
            "warm grade, composed as one clean group along the bottom-right of the frame and "
            "cropped by the bottom edge, the top half left open for a headline."
        ),
    },
]

# ── AI Answering — from Ad Creatives / Q1 Content 2026 ────────────────────────
# Palette: hot magenta #E6007E, blue #2196D9 → violet #7B5BD6 sweep, dark plum
# #0A0A0F/#150812, blush #F8D8E8. (prompt-library/ai-answering/) — a dynamic
# Firestore brand; keyed by its pack id so it lights up whenever the brand is
# registered.

_AI_ANSWERING_STAGE1 = [
    {
        "id": "F",
        "title": "Dark Plum Neon Glow",
        "desc": "From past creatives — near-black plum field with hot magenta neon glows.",
        "css_gradient": "radial-gradient(ellipse at 70% 30%, #3A0E2E 0%, #150812 55%, #0A0A0F 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio dark premium tech background. A near-black plum field "
            "grounded in #0A0A0F and #150812, lifted by soft neon magenta #E6007E glows — one "
            "diffuse bloom in the upper-right and a fainter #C4108F haze along the bottom edge — "
            "like light from an unseen interface. Deep, cinematic, high-contrast, ready for white "
            "and pink text. Soft, seamless blending with no harsh edges. Ultra-smooth, high "
            "resolution, no noise, no text."
        ),
    },
    {
        "id": "G",
        "title": "Blue-Violet-Magenta Sweep",
        "desc": "From past creatives — the signature blue → violet → hot magenta gradient.",
        "css_gradient": "linear-gradient(120deg, #2196D9 0%, #7B5BD6 50%, #E6007E 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio immersive abstract background gradient. A smooth diagonal "
            "sweep flowing from bright blue #2196D9 in the upper-left through rich violet #7B5BD6 "
            "at the center into hot magenta #E6007E in the lower-right, each transition softly "
            "feathered with a subtle luminous core along the diagonal. Vivid, energetic, modern "
            "AI-product feel. Soft, seamless blending with no harsh edges. Minimalist, cinematic, "
            "ultra-smooth gradient texture, high resolution, no noise, no text."
        ),
    },
    {
        "id": "H",
        "title": "Blush Light Theme",
        "desc": "From past creatives — airy white-to-blush-pink light background.",
        "css_gradient": "linear-gradient(180deg, #FFFFFF 0%, #F8D8E8 70%, #F5B8D0 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio airy light background. Pure white #FFFFFF at the top "
            "melting gently into soft blush pink #F8D8E8 and warmer #F5B8D0 toward the bottom, "
            "with one whisper-soft magenta #E6007E glow feathered into a lower corner at low "
            "opacity. Friendly, clean, modern SaaS light theme with plenty of room for dark text. "
            "Soft, seamless blending with no harsh edges. Minimalist, ultra-smooth, high "
            "resolution, no noise, no text."
        ),
    },
    {
        "id": "I",
        "title": "Curtain Panels",
        "desc": "From past creatives — black vertical curtain panels with a cool sheen.",
        "css_gradient": "linear-gradient(90deg, #0A0A0F 0%, #1C1C24 25%, #0A0A0F 50%, #1C1C24 75%, #0A0A0F 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio dark editorial background of tall vertical curtain-like "
            "panels. Soft matte black #0A0A0F folds alternating with slightly lighter charcoal "
            "ridges, each fold catching a faint cool sheen — a hint of sky blue #2E9FD9 on one "
            "ridge and a hint of magenta #E6007E on another — like stage curtains under dim "
            "colored light. Elegant, minimal, theatrical. Soft, seamless blending with no harsh "
            "edges. Ultra-smooth, high resolution, no noise, no text."
        ),
    },
    {
        "id": "J",
        "title": "Ghosted Spark Watermark",
        "desc": "From past creatives — magenta gradient with a giant ghosted four-point spark shape.",
        "css_gradient": "linear-gradient(135deg, #150812 0%, #3A0E2E 55%, #E6007E 100%)",
        "prompt": (
            "Create a 16:9 aspect ratio premium brand background. A deep plum-to-magenta gradient "
            "sweeping from #150812 in the upper-left through #3A0E2E into a rich #E6007E corner "
            "glow at the lower-right, with one giant, abstract four-point sparkle/star shape "
            "ghosted at about 8% opacity, oversized and bleeding off the right edge like a "
            "watermark. Sleek, modern AI-product mood. Soft, seamless blending with no harsh "
            "edges. Ultra-smooth, high resolution, no noise, no text."
        ),
    },
]

_AI_ANSWERING_STAGE2 = [
    {
        "id": "G",
        "title": "Dashboard Mockup",
        "desc": "From past creatives — floating call-analytics dashboard card, unreadable data.",
        "angle": "product / platform",
        "category": "object",
        "subject": (
            "A sleek floating SaaS dashboard mockup card in dark mode with rounded corners and a "
            "soft drop shadow — suggesting call analytics with a small line chart, a few stat "
            "tiles and a list of call rows, every label rendered as soft unreadable placeholder "
            "bars, magenta and violet accent highlights. Tilted slightly in perspective in the "
            "right third of the frame, left side open."
        ),
    },
    {
        "id": "H",
        "title": "Magenta-Graded Caller",
        "desc": "From past creatives — businesswoman on a phone call, heavy magenta grade.",
        "angle": "human moment",
        "category": "people",
        "subject": (
            "A photorealistic businesswoman holding a smartphone to her ear mid-call, engaged "
            "expression, shot with shallow depth of field and color-graded heavily toward magenta "
            "and plum tones so she melts into the brand world, occupying the right side of the "
            "frame and cropped by the right edge, left side open."
        ),
    },
    {
        "id": "I",
        "title": "3D Receptionist Avatar",
        "desc": "From past creatives — friendly pink-haired 3D receptionist with a headset.",
        "angle": "brand mascot",
        "category": "people",
        "subject": (
            "A friendly stylized 3D-rendered receptionist avatar with pink hair and a slim "
            "headset, soft studio lighting, smooth modern character design with a warm smile, "
            "shown from the chest up and placed small in the lower-right corner of the frame, "
            "the rest of the frame left open."
        ),
    },
    {
        "id": "J",
        "title": "Neon Phone Tile",
        "desc": "From past creatives — glowing rounded phone-icon tile with a notification badge.",
        "angle": "missed calls pain",
        "category": "object",
        "subject": (
            "A single glossy rounded-square app tile with a simple white phone handset icon, "
            "glowing with neon blue and magenta rim light against soft darkness, one small blank "
            "notification badge at its corner, floating in the lower-right of the frame with a "
            "gentle reflection beneath, generous open space above."
        ),
    },
    {
        "id": "K",
        "title": "Human & Robot Hands",
        "desc": "From past creatives — cinematic CGI human and robot hands reaching to touch.",
        "angle": "AI + human balance",
        "category": "scene",
        "subject": (
            "A cinematic CGI scene of a human hand and a sleek white robotic hand reaching toward "
            "each other, fingertips almost touching, dramatic magenta and violet rim lighting "
            "against soft darkness, shallow depth of field, composed along the lower band of the "
            "frame with open space above."
        ),
    },
]

# Brand pack id → library extension. Dynamic brands (e.g. AI Answering) match
# by whatever id their Firestore gd_spec registers under.
LIBRARY: dict[str, dict] = {
    "legalsoft": {"stage1": _LEGALSOFT_STAGE1, "stage2": _LEGALSOFT_STAGE2},
    "medvirtual": {"stage1": _MEDVIRTUAL_STAGE1, "stage2": _MEDVIRTUAL_STAGE2},
    "remote_attorneys": {"stage1": _REMOTE_ATTORNEYS_STAGE1, "stage2": _REMOTE_ATTORNEYS_STAGE2},
    "ai-answering": {"stage1": _AI_ANSWERING_STAGE1, "stage2": _AI_ANSWERING_STAGE2},
    "ai_answering": {"stage1": _AI_ANSWERING_STAGE1, "stage2": _AI_ANSWERING_STAGE2},
}


def _inline_name(brand_id: str, variant_id: str) -> str:
    return f"stage1_library_{brand_id}_{variant_id}.txt"


def extend_pack(pack):
    """Return ``pack`` with its brand's library variants appended, or the pack
    unchanged when no library entry exists. Append-only: an id already present
    in the pack is skipped (canonical variants always win), and Stage-1 prompt
    texts are added to ``inline_prompts`` so no ``.txt`` file or frozen hash is
    touched."""
    lib = LIBRARY.get(pack.id)
    if not lib:
        return pack

    taken1 = {v["id"] for v in pack.stage1_variants}
    stage1_new, inline_new = [], {}
    for item in lib.get("stage1", []):
        if item["id"] in taken1:
            continue
        fname = _inline_name(pack.id, item["id"])
        stage1_new.append({
            "id": item["id"], "prompt_file": fname, "title": item["title"],
            "desc": item["desc"], "css_gradient": item["css_gradient"],
        })
        inline_new[fname] = item["prompt"]

    taken2 = {v["id"] for v in pack.stage2_variants}
    stage2_new = [
        {k: v for k, v in item.items()}
        for item in lib.get("stage2", []) if item["id"] not in taken2
    ]

    if not stage1_new and not stage2_new:
        return pack
    return dataclasses.replace(
        pack,
        stage1_variants=[*pack.stage1_variants, *stage1_new],
        stage2_variants=[*pack.stage2_variants, *stage2_new],
        inline_prompts={**(pack.inline_prompts or {}), **inline_new},
    )
