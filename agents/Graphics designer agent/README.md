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
│   ├── prompts/                   # 10 canonical prompts, byte-frozen (§2.1)
│   ├── prompts.py                 # loader + SHA-256 integrity baseline (§9.1)
│   ├── tokens.py                  # the ONLY allowed prompt edits (§6)
│   ├── variants.py                # UI metadata: concepts, fonts, AR, brand kit
│   ├── providers.py               # mock (offline) + OpenRouter image providers
│   ├── compositor.py              # deterministic Stage-4 logo composite (§5.4)
│   ├── runs.py                    # run persistence + artifacts + manifest log
│   ├── pipeline.py                # state machine: generate / approve / back (§4)
│   ├── suggestions.py             # approval-gated agent suggestion layer (§7)
│   ├── graph.py                   # legacy run_agent shim
│   └── state.py
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
