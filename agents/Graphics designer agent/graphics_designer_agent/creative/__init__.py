"""Creative Agent — the dedicated rail for creatives beyond standard social posts.

When a user picks a brochure, presentation, carousel or blog visual, the request
is routed here instead of the standard 4-stage social editor (``pipeline.py``).
This package owns that rail end-to-end:

- ``types``            — the 4-step model, routing, output formats, the mandatory
                         autonomous-mode warning text.
- ``planner``          — turns a brief + retrieved brand precedent into a
                         *reviewable* plan (carousel frames, deck slides, brochure
                         sections, blog cover + in-article images).
- ``document_builder`` — the structured layout engine that renders the plan into
                         a real PDF (reportlab), PPTX (python-pptx) or PNG set
                         (Pillow), grounded in the brand's palette + fonts.
- ``runs``             — run persistence + the decision log (every agent decision
                         is recorded so the user can audit the rationale).
- ``pipeline``         — orchestration: manual step-by-step or fully autonomous,
                         with a one-click human override at any point.

Everything is additive and offline-first: heavy export deps (reportlab,
python-pptx, PyMuPDF) are imported lazily, so the package still imports and the
core rail still runs without them.
"""

from __future__ import annotations
