# Graphic Designer Agent — User Manual, SOP & Specification

> A plain-language guide to creating on-brand advertising creatives with the
> Graphic Designer Studio. If you have never used this tool before, read
> **Parts 1–3**. If you just want the click-by-click steps, jump to
> **Part 4 (The Standard Operating Procedure)**. Technical details for
> developers and admins are in **Part 7 (Specification)**.

---

## Table of contents

1. [What this agent does](#1-what-this-agent-does)
2. [The big ideas (read this once)](#2-the-big-ideas-read-this-once)
3. [Before you start](#3-before-you-start)
4. [The Standard Operating Procedure (step by step)](#4-the-standard-operating-procedure-step-by-step)
   - [Stage 1 — Gradient base](#stage-1--gradient-base)
   - [Stage 2 — Photo element](#stage-2--photo-element)
   - [Stage 3 — Text overlay](#stage-3--text-overlay)
   - [Stage 4 — Logo composite (final)](#stage-4--logo-composite-final)
5. [Letting the agent help you](#5-letting-the-agent-help-you)
6. [Tips, troubleshooting & FAQs](#6-tips-troubleshooting--faqs)
7. [Specification (for developers & admins)](#7-specification-for-developers--admins)
8. [Glossary](#8-glossary)

---

## 1. What this agent does

The **Graphic Designer Agent** turns a few choices and lines of copy into a
finished, **on-brand advertising image** — the kind you'd run on Instagram,
LinkedIn, or a landing page.

It is built for one brand (a US legal-tech / "Virtual Legal Staff" company), so
the **colors, gradient, and font are already locked in**. You don't need design
skills: you pick from ready-made options, type your headline, choose where
things sit, and the tool generates the picture for you.

It works like an assembly line in **four stages**, and **you approve each stage
before moving on**:

```
 Stage 1            Stage 2             Stage 3            Stage 4
 ┌─────────┐        ┌─────────┐         ┌─────────┐        ┌─────────┐
 │ Gradient│  ───▶  │  Photo  │   ───▶  │  Text   │  ───▶  │  Logo   │  ───▶  ✅ Final image
 │  base   │        │ element │         │ overlay │        │composite│
 └─────────┘        └─────────┘         └─────────┘        └─────────┘
  background          add a               add headline       drop your
  color blend         person/object       + CTA button       logo on top
```

Each stage builds **on top of the image you approved in the stage before it** —
so the final picture is the sum of all four steps.

---

## 2. The big ideas (read this once)

Five concepts explain almost everything about how the tool behaves:

1. **You are always in control (human-in-the-loop).**
   The agent *suggests*; it never decides. Nothing moves forward until **you
   click Approve**. You can regenerate as many times as you like and pick the
   attempt you prefer.

2. **The brand is locked.**
   The gradient palette (white → light blue → royal blue `#1746A2`), the text
   color options, and the font family (**Causten**) are fixed so every creative
   stays on-brand. You choose *variations* (which gradient style, which Causten
   weight, which text color from the allowed palette) — never off-brand values.

3. **Each stage chains the previous one.**
   Stage 2 is drawn onto your approved Stage 1 image, Stage 3 onto your approved
   Stage 2, and so on. This is why **approving** matters — it locks the input
   for the next stage.

4. **Aspect ratio is chosen once, at Stage 1.**
   Pick your shape (e.g. Instagram Portrait 4:5) at the very start. It then
   stays the same through all four stages so the canvas size never jumps. To
   change it later you must go back to Stage 1 (see [Going back](#going-back-and-revising)).

5. **Quality is high by design.**
   Backgrounds render at **2K** and the photo/text/final stages render at **4K**,
   at the exact aspect ratio you chose. You don't set this — it's automatic.

---

## 3. Before you start

You need three things:

| You need… | Details |
|---|---|
| **The app running** | Backend on `http://localhost:8080`, frontend on `http://localhost:3000`. See [How to start the app](#how-to-start-the-app) in Part 7. |
| **An account / sign-in** | Open `http://localhost:3000`, sign in, then open the **Graphic Designer** agent. |
| **Your logo file** (for Stage 4) | PNG, JPG, or SVG. A **transparent PNG** works best. |

> **Real images vs. preview placeholders.** If the system has an image-generation
> key configured, you get real AI-generated creatives. If not, it falls back to a
> **mock** mode that draws simple brand-colored placeholders — fine for learning
> the flow, but not real artwork. Ask your admin which mode you're in, or see
> [Provider modes](#provider-modes) in Part 7.

### The screen layout

When you open the studio you'll see three areas:

- **Left rail** — the 4-stage stepper (where you are), the brand kit, and a
  prompt-integrity panel.
- **Center** — the canvas: your current image, past attempts, and the
  Approve / Regenerate buttons.
- **Right rail** — the controls for the current stage, plus the agent's
  suggestions and a "prompt audit" you can expand to see exactly what was sent
  to the image model.

---

## 4. The Standard Operating Procedure (step by step)

A complete run is: **Stage 1 → approve → Stage 2 → approve → Stage 3 → approve →
Stage 4 → approve → done.** Here is each stage in detail.

---

### Stage 1 — Gradient base

**Goal:** create the brand background (a smooth color blend) at the shape you want.

1. **Pick your aspect ratio.** Choose the shape your ad needs:
   - **4:5 — Instagram Portrait** (default, 1080×1350)
   - **1:1 — Square** (1080×1080)
   - **9:16 — Story / Reel** (1080×1920)
   - **16:9 — Landscape** (1920×1080)

   > This is the one moment to get the shape right. It locks for the rest of the
   > run. *(Tip: the agent can recommend a shape based on where you'll post — see
   > [Letting the agent help you](#5-letting-the-agent-help-you).)*

2. **Pick a gradient style.** There are 12 ready-made styles (A–L), each with a
   little preview chip and a name, for example:
   - **A · Diagonal Sweep** — white top-left flowing into royal blue bottom-right
   - **B · Inverted Horizon** — blue on top dissolving to white at the bottom
   - **D · High-Key Skyline**, **F · Cinematic Night Vegas**, **K · Aerial Map Sky**, etc.

3. **Click "Generate gradient."** Wait a few seconds for the image to appear in
   the center.

4. **Not happy? Regenerate.** Click regenerate to get a fresh take of the same
   style, or pick a different style and generate again. Every attempt is kept —
   you can click any past attempt to compare.

5. **Approve** the one you like. This unlocks Stage 2.

**What's new / good to know**
- The background now renders at **your chosen aspect ratio** (it used to always
  be widescreen). It comes out at **2K** resolution and feeds the next stages.

---

### Stage 2 — Photo element

**Goal:** add **one** subject — a person, object, building, or scene — onto your
approved background, blended so it looks like a single photograph.

1. **Browse the element library.** Elements are grouped by category — **people,
   object, flatlay, architecture, scene** — for example:
   - **A · Solo Virtual Assistant** (warm, efficiency)
   - **B · Honeycomb Trio** (social proof)
   - **D · Partner's Corner Office, 11:47 PM** (pain-point story, no people)
   - **O · Hazy Glass Skyline** (authority / scale)
   - …and many more (A–S).

2. **Pick one element** that fits your message. Each card tells you its "angle"
   (e.g. authority, warmth, trust) so you can match it to your campaign.

3. **Click "Generate photo composite."** The tool places the subject onto your
   exact Stage 1 background and **blends** the two — matching light, color, and
   edges — leaving open space for your text later.

4. **Regenerate** if the blend or pose isn't right, then **Approve**.

**What's new / good to know**
- The result is generated at **4K** and at your locked aspect ratio, so it looks
  finished (not "raw") and never the wrong shape.
- The agent can **"explore"** the wider library and propose fresh, less-obvious
  elements if you want ideas — see Part 5.

---

### Stage 3 — Text overlay

**Goal:** add your headline, sub-text, and CTA (call-to-action) button on top of
the photo — styling **each piece independently**.

This stage has two halves: **(A) the words**, and **(B) how each word-block looks
and where it sits**.

#### A. Write & approve your copy

There are five text pieces. Type each one, then click **Approve** on it. (You
must approve **all five** before you can generate.)

| Piece | What it is | Rules |
|---|---|---|
| **Headline** | The big hook line | ≤ 9 words |
| **Highlight** | A phrase *inside* the headline that gets the brand gradient | Must be an exact part of the headline |
| **Sub-text line 1** | First short value statement | ≤ 70 characters |
| **Sub-text line 2** | Second short value statement | ≤ 70 characters |
| **CTA** | The button text | ≤ 4 words |

> **Why approve each line?** It creates an audit trail of exactly what you
> authorized, and it's the gate that unlocks generation. The agent can suggest
> headlines and CTAs ("Suggest hooks") — but you still approve them.

#### B. Style each element (the per-element control bars)

Below the copy you'll find a **"Per-element styling"** section. **Every element
gets its own row** with up to three controls:

| Element | Font | Color | Placement |
|---|---|---|---|
| **Heading** (headline) | ✅ any Causten weight | ✅ Dark / Brand gradient / White | ✅ Left, Right, Center, Top, Bottom |
| **Hook / highlight** | ✅ | ✅ | — (it sits inside the heading) |
| **Sub-heading 1** | ✅ | ✅ | ✅ |
| **Sub-heading 2** | ✅ | ✅ | ✅ |
| **CTA button** | ✅ | 🔒 locked orange button | ✅ (Below text, Left, Center, Right, Above text) |

- **Font** — any weight/style in the **Causten** family (Thin → Black, upright
  or oblique). The family stays locked; only the variation changes.
- **Color** — three swatches per text element:
  - **Dark** (`#0F0F0F`, the default)
  - **Brand gradient** (`#86AFFE → #2653AB`)
  - **White** (`#FFFFFF`) — great when the photo is dark
- **Placement** — a small button bar to position that element on the image.

A **live preview** at the top of the panel updates as you change fonts, colors,
and placements, so you can see roughly how it'll look.

#### Generate & approve

Click **"Generate text overlay."** The tool writes the text **directly onto the
photo** — **no box, panel, or shaded rectangle behind the text** (the only filled
shape allowed is the orange CTA button). Regenerate if needed, then **Approve**.

**What's new / good to know**
- Per-element fonts, colors, and placement are all new — previously one font and
  one placement applied to everything.
- The "ugly box behind the text" problem is fixed: the instructions now forbid
  any backing panel so your photo stays fully visible.
- Renders at **4K** so the text is crisp.

---

### Stage 4 — Logo composite (final)

**Goal:** place your logo on the approved Stage-3 image to finish the creative.

1. **Upload your logo** (PNG / JPG / SVG; transparent PNG preferred).

2. A **live preview** appears — your Stage-3 image with a dashed box showing
   exactly where the logo will land and how big it will be.

3. **Position it** with the **placement guide** — a 3×3 grid:
   ```
   Top-left      Top-center      Top-right
   Middle-left   Center          Middle-right
   Bottom-left   Bottom-center   Bottom-right
   ```

4. **Size it** with the **resizer bar** (a slider). It sets the logo width as a
   percentage of the image (4%–60%); the readout shows both the **%** and the
   exact **pixels**. The height adjusts automatically so the logo never stretches.

5. **Fine-tune in pixels** with the **Fine refinement** controls:
   - **Width** — type an exact pixel width
   - **Margin %** — distance from the edges
   - **Nudge X / Nudge Y** — move the logo by exact pixels in any direction
   - **Reset placement** — go back to the default (top-left, small, 4% margin)

6. **Choose how it's placed:**
   - **Default (recommended): deterministic compositor.** Places your logo at
     the exact spot and size you set, and **leaves every other pixel of the image
     untouched** — perfectly crisp, no surprises.
   - **AI compositor (optional checkbox):** lets the AI blend the logo. It
     follows your placement as a *guide* but is **not pixel-exact**. Use only if
     you want the logo to feel "painted into" the scene.

7. **Click "Composite logo,"** review, regenerate if needed, and **Approve**.
   That's your finished creative. Save it from the canvas (right-click the image
   → Save, or use the download control if shown).

---

### Going back and revising

Made a decision you want to change? Use **Back** to return to any earlier stage.

> ⚠️ **Important:** going back to a stage **clears the approvals of every stage
> after it**, because they were built on the old image. Example: going back to
> Stage 1 to change the aspect ratio will require you to redo Stages 2–4.

Nothing is ever deleted — every past attempt remains, and you can re-approve an
earlier attempt at any time.

---

## 5. Letting the agent help you

The agent offers **optional suggestions** at each step. They always appear as
*proposals* — you choose whether to use them. Available helpers:

| Helper | What it does | Where |
|---|---|---|
| **Onboarding questions** | 3 quick questions (campaign goal, audience, emotional angle) that tune the recommendations | Start of a run |
| **Concept recommendation** | Suggests the best Stage-2 element for your answers, with a one-line reason | Stage 2 |
| **Explore elements** | "Plays" with the wider library and proposes fresh, less-obvious picks + a wildcard | Stage 2 |
| **Aspect-ratio recommendation** | Suggests a shape based on where you'll post (feed → 4:5, story → 9:16, etc.) | Stage 1 |
| **Hook ideas** | Suggests headlines (with the highlight phrase) and CTAs tailored to your chosen concept's angle | Stage 3 |
| **Font recommendation** | Suggests a Causten weight that fits the concept | Stage 3 |
| **QA critique** | A short "things to check" note for the current stage (e.g. "check the gradient for banding") | Any stage |

Remember: **a suggestion only reaches the image when you approve it.** This keeps
a clean record of every value you authorized.

---

## 6. Tips, troubleshooting & FAQs

**Best-practice tips**
- **Choose the aspect ratio first and deliberately** — changing it later means
  redoing later stages.
- **Leave room for text** — in Stage 2, elements are designed to keep part of
  the frame empty. Don't fight that; place your text in the open area in Stage 3.
- **High-contrast text** — on a dark photo, set the heading/sub-text color to
  **White**; on a light area, keep it **Dark**. Use the **Brand gradient** on the
  highlight phrase for that signature look.
- **Logo:** a transparent PNG on the **top-left** or **bottom-right** at ~12–20%
  width usually reads best. Use **Nudge X/Y** for final polish.
- **Compare attempts** — generate a few, then pick; you never lose earlier ones.

**Troubleshooting**

| Problem | Likely cause & fix |
|---|---|
| Images look low-resolution / "2K-ish" | You may be in **mock** mode (placeholders), or no image key is configured. Ask your admin / see [Provider modes](#provider-modes). Real runs render at 2K (Stage 1) and 4K (Stages 2–4). |
| The wrong shape comes out | Confirm the aspect ratio you picked in Stage 1; it drives all stages. |
| A box/panel appears behind the text | This is fixed in the current version — make sure the backend is running the latest code (restart it; see Part 7). |
| "Approve all tokens to generate" won't let me proceed (Stage 3) | You must **Approve all five** copy fields first. |
| "Aspect ratio is locked after Stage 1" | Correct — use **Back** to Stage 1 to change it (this resets later stages). |
| Logo upload rejected | Use PNG, JPG, or SVG. Very large or corrupt files may fail — try a clean PNG. |
| Logo looks fuzzy or shifted with AI compositor | The AI path isn't pixel-exact. Uncheck **AI compositor** to use the precise deterministic placement. |
| Page says 401 / not signed in | Sign in again at `http://localhost:3000`. |

**FAQs**
- *Can I use a different font or color?* Only within the brand: any **Causten**
  weight, and text colors **Dark / Brand gradient / White**. The CTA button is
  always the brand orange.
- *Where do my images go?* Every run and every attempt is saved on the server
  (see [Where runs are stored](#where-runs-are-stored)).
- *Can I change the headline after approving?* Yes — edit it and approve again,
  then regenerate Stage 3.

---

## 7. Specification (for developers & admins)

### Architecture in one paragraph

A FastAPI router (`app/routers/graphics_designer.py`) exposes the studio API
under `/api/gd`. The creative logic lives in the standalone Python package
`graphics_designer_agent`. Image generation goes through a **provider**
abstraction; the real provider calls **OpenRouter** with Google's
**Gemini 3 Pro Image (Nano Banana Pro)** model. The Next.js component
`newfrontend/components/console/GraphicsStudio.tsx` is the UI.

### Core design rules

- **Immutable prompts.** The master prompts live as `.txt` files under
  `graphics_designer_agent/prompts/` and are **byte-frozen** with SHA-256 hashes
  in `prompts.py` (a test enforces them). They are never edited at runtime; the
  only permitted change is **token substitution** (`tokens.py`) — replacing
  whitelisted placeholder strings with approved values.
- **Human-in-the-loop / approval gate.** Suggestions are returned as proposals;
  a value is only logged + used after an explicit approve (`manifest_log`).
- **Mandatory image chaining.** Stages 2–4 require the approved upstream image as
  a reference (`pipeline.reference_for`).
- **Deterministic Stage 4.** The logo compositor (`compositor.py`) guarantees
  every pixel outside the logo box is byte-identical to the input.

### The four stages (technical)

| Stage | Prompt file | Output resolution | Key inputs |
|---|---|---|---|
| 1 Gradient | `stage1_gradient_{A..L}.txt` | **2K** | gradient variant, aspect ratio |
| 2 Photo | `stage2_element_blend.txt` | **4K** | element subject (`[SUBJECT]`), AR, Stage-1 image |
| 3 Text | `stage3_text_overlay.txt` | **4K** | copy tokens + per-element styles, Stage-2 image |
| 4 Logo | `stage4_logo_composite.txt` (AI) or Pillow (deterministic) | **4K** | logo file, logo layout, Stage-3 image |

Resolution per stage is set in `pipeline.STAGE_IMAGE_SIZE = {1:"2K", 2:"4K", 3:"4K", 4:"4K"}`.
Aspect ratio + resolution are sent to the model via OpenRouter's `image_config`
(`aspect_ratio`, `image_size`).

### Aspect ratios

`4:5` (1080×1350, default) · `1:1` (1080×1080) · `9:16` (1080×1920) · `16:9` (1920×1080).
Chosen at Stage 1, then locked (`/config` rejects changes once past Stage 1).

### Stage 3 per-element styling model

Stored in `run.config.element_styles`, keyed by element
(`headline`, `highlight`, `subtext1`, `subtext2`, `cta`). Each element may have:
- `font` — any name in the Causten family (`variants.FONTS`)
- `color` — `dark` / `gradient` / `white` (text elements only; CTA is locked)
- `placement` — a text-placement key (or CTA-placement key for the CTA)

The prompt carries per-element markers (`[HEADLINE_FONT]`, `[HEADLINE_COLOR]`,
`[HEADLINE_PLACEMENT]`, `[HIGHLIGHT_*]`, `[SUBTEXT1_*]`, `[SUBTEXT2_*]`,
`[CTA_FONT]`, `[CTA_PLACEMENT]`) which `tokens.substitute_stage3` fills with
resolved phrases. The prompt explicitly forbids any box/panel behind the text.

### Stage 4 logo layout model

Stored in `run.config.logo_layout`:
- `position` — one of the 3×3 grid keys (`top-left` … `bottom-right`)
- `size_pct` — logo width as % of canvas width (`null` = aspect-aware default 20/25/15%)
- `margin_pct` — edge inset (% of width, default 4)
- `offset_x`, `offset_y` — fine pixel nudges

`compositor.logo_placement` computes the box; `composite_logo` renders it. The
frontend mirrors this math in `computeLogoBox` for the live preview.

### Provider modes

Selected by `GD_IMAGE_PROVIDER` (env): `mock`, `openrouter`, or unset (auto).
Auto uses **OpenRouter** when `OPENROUTER_API_KEY` (or the app setting) is
present, otherwise the offline **mock** (flat brand gradients/placeholders).
Image model: `google/gemini-3-pro-image-preview` (configurable via
`OPENROUTER_IMAGE_MODEL`).

### API reference (`/api/gd`, all auth-gated)

| Method & path | Purpose |
|---|---|
| `GET /gd/config` | Static studio config: variants, fonts, colors, placements, logo grid, aspect ratios, onboarding questions |
| `GET /gd/prompts` | Prompt integrity report (hash check) |
| `POST /gd/runs` | Create a new run |
| `GET /gd/runs/{id}` | Fetch a run |
| `POST /gd/runs/{id}/config` | Update config (font, AR, `element_styles`, `logo_layout`, tokens, approvals, AI toggle) |
| `POST /gd/runs/{id}/generate` | Generate an attempt for stage 1–3 |
| `POST /gd/runs/{id}/stage4` | Upload logo + composite (deterministic or AI) |
| `POST /gd/runs/{id}/approve` | Approve an attempt for a stage |
| `POST /gd/runs/{id}/back` | Return to a stage (clears downstream approvals) |
| `GET /gd/runs/{id}/prompt` | Build the exact prompt for a stage without generating (audit) |
| `POST /gd/runs/{id}/suggest` | Agent suggestions: `concept`/`explore`/`aspect_ratio`/`hooks`/`font`/`qa` |
| `GET /gd/runs/{id}/artifact/{path}` | Stream a generated PNG |

### Where runs are stored

Under `GD_RUNS_DIR` (default `<agent>/runs/`), one folder per run containing
`run.json` (the full manifest) and `stage-<n>/<variant>-<attempt>.png` artifacts.
Nothing is deleted — any past attempt can be re-approved.

### How to start the app

From the project root (`C:\Users\ACER\Desktop\ghi`), in two terminals:

```powershell
# Terminal 1 — backend (http://localhost:8080)
powershell -ExecutionPolicy Bypass -File .\start-backend.ps1

# Terminal 2 — frontend (http://localhost:3000)
powershell -ExecutionPolicy Bypass -File .\start-frontend.ps1
```

Then open `http://localhost:3000`, sign in, and click the **Graphic Designer**
agent.

> **Note for developers:** the agent package lives outside `app/`, so the backend
> does **not** auto-reload it. After editing `graphics_designer_agent/*`, restart
> the backend (the start script frees port 8080 automatically). Run the tests
> with `cd "backend\agents\Graphics designer agent"; python -m pytest -q`.

### Key files

```
backend/agents/Graphics designer agent/graphics_designer_agent/
  prompts/                 # immutable master prompts (byte-frozen)
  prompts.py               # prompt loading + SHA-256 integrity baseline
  tokens.py                # token substitution (the only permitted prompt edits)
  variants.py              # UI metadata: gradients, elements, fonts, colors, logo grid
  runs.py                  # run/config creation + persistence
  pipeline.py              # the stage state machine + generation
  providers.py             # mock + OpenRouter image providers
  compositor.py            # deterministic Stage-4 logo compositor
  suggestions.py           # the agent's proposal layer
backend/app/routers/graphics_designer.py   # the /api/gd API
backend/app/services/openrouter.py         # OpenRouter image/LLM calls
newfrontend/components/console/GraphicsStudio.tsx   # the studio UI
newfrontend/app/gd.css                              # studio styles
```

---

## 8. Glossary

- **Aspect ratio (AR)** — the shape/proportions of the image (e.g. 4:5 portrait).
- **Attempt** — one generated image. Each generate/regenerate creates a new
  attempt; all are kept.
- **Approve** — locking the attempt you chose so the next stage can use it.
- **Chaining** — each stage builds on the approved image from the previous stage.
- **CTA** — "call to action," the button text (e.g. "Book a Free Consultation").
- **Deterministic compositor** — the pixel-exact logo placement (default in Stage 4).
- **Element** — the Stage-2 subject (a person, object, building, or scene).
- **Highlight** — the part of the headline shown in the brand gradient color.
- **Mock provider** — offline placeholder mode (no API key needed).
- **Token** — a piece of copy (headline, sub-text, CTA) that fills a slot in the prompt.
- **Variant** — a ready-made option (gradient style A–L, element A–S).

---

*Last updated for the version that adds: per-stage aspect-ratio + 4K rendering,
Stage-3 per-element font/color/placement with no text box, and Stage-4 logo
placement grid + resizer + pixel fine-tuning.*
