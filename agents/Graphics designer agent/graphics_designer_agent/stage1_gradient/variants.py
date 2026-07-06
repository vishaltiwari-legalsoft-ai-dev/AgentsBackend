"""Stage-1 UI metadata: the curated gradient concept cards.

None of the copy here is ever injected into a prompt (spec §5.2) — prompt
content lives exclusively in ``../prompts/stage1_gradient_*.txt``. ``css_gradient``
is only the UI swatch preview.
"""

from __future__ import annotations

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
