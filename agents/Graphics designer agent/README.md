# Graphics Designer Agent (Marketing Department)

Legal Soft's **4-stage AI ad-creative pipeline**: gradient → photo → text → logo,
with a human-in-the-loop approval gate at every stage. Full specification in
[`agent.md`](agent.md). Built per that spec (Python/FastAPI instead of the spec's
Node suggestion; Stage-4 compositing via Pillow instead of Sharp).

## Layout

```
agents/Graphics designer agent/
├── agent.md                       # the immutable build spec
├── graphics_designer_agent/       # importable package (underscore name)
│   ├── stage1_gradient/           # STEP 1 — background foundation
│   │   ├── prompting.py           #   Stage-1 AR substitution (§6)
│   │   └── variants.py            #   gradient concept cards (UI)
│   ├── stage2_element/            # STEP 2 — subject/element blend
│   │   ├── prompting.py           #   [SUBJECT] substitution + 9-cell placement
│   │   └── variants.py            #   subject library + placement grid (UI)
│   ├── stage3_text/               # STEP 3 — deterministic text overlay
│   │   ├── text_overlay.py        #   Pillow renderer (fonts, CTA pill, gradients)
│   │   ├── layout.py              #   run config → resolved layer list
│   │   ├── render.py              #   engine dispatch (Pillow vs Konva service)
│   │   ├── render_contract.py     #   render-request contract for the service
│   │   ├── elements.py            #   Canva-style element library
│   │   ├── shapes.py / icons.py   #   2D shapes + infographic glyphs
│   │   ├── prompting.py           #   content tokens + audit prompt (§6.3)
│   │   └── style_options.py       #   UI options: placements, colours, sizes
│   ├── stage4_logo/               # STEP 4 — logo composite
│   │   ├── compositor.py          #   deterministic Pillow composite (§5.4)
│   │   └── options.py             #   placement grid + slider bounds (UI)
│   ├── prompts/                   # canonical prompts, byte-frozen (§2.1)
│   ├── prompts.py                 # loader + SHA-256 integrity baseline (§9.1)
│   ├── tokens.py                  # shared substitution engine + AR presets (§6)
│   ├── variants.py                # shared brand kit: locked colours, fonts
│   ├── providers.py               # mock (offline) + OpenRouter image providers
│   ├── runs.py                    # run persistence + artifacts + manifest log
│   ├── pipeline.py                # state machine: generate / approve / back (§4)
│   ├── suggestions.py             # approval-gated agent suggestion layer (§7)
│   ├── registry.py                # multi-brand packs (+ templated_brands, brands/)
│   ├── reference_library.py       # brand precedent ingestion + retrieval
│   └── creative/                  # multi-frame rail (brochure/PPTX/carousel/blog)
└── tests/                         # §9 acceptance criteria (pytest)
```

## API

Exposed by `app/routers/graphics_designer.py` under `/api/gd/*`
(create run → generate → approve, per stage; suggestions; prompt audit;
artifact streaming). The frontend studio lives at
`newfrontend/components/console/GraphicsStudio.tsx`.

## Importing

`app/__init__.py` puts this folder on `sys.path`:

```python
from graphics_designer_agent import pipeline, suggestions, variants
```

## Tests

```bash
cd "backend/agents/Graphics designer agent" && python -m pytest -q
```

Covers prompt-hash immutability (§9.1), token isolation (§9.2), image chaining
(§9.3), and Stage-4 pixel preservation (§9.5).

## Notes

- The parent folders contain spaces, so the package uses an underscore name.
- `GD_IMAGE_PROVIDER=mock` (default without an OpenRouter key) renders brand-accurate
  placeholders so the whole pipeline is demoable offline.
- The Dockerfile copies `agents/` into the image.
