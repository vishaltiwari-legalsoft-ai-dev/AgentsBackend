# BUILD SPEC — Legal Soft 4-Stage AI Ad Creative Pipeline

> **Audience:** Claude Opus (Claude Code, VS Code). This document is the complete, self-contained specification. Build exactly what is described. Where this spec says a prompt is IMMUTABLE, do not paraphrase, reword, reformat, "improve," shorten, or re-punctuate it — store and send it byte-for-byte, with only the whitelisted token substitutions defined in §6.

---

## 1. What this agent  is

A local agent  that runs a **4-stage image-generation pipeline** for producing Legal Soft social media ad creatives:

| Stage | Name | Input | Output |
|---|---|---|---|
| 1 | Gradient base | brand-locked gradient prompt (variant A or B) | gradient background image |
| 2 | Photo element composite | Stage 1 image + photo prompt (variant A–E) | unified cinematic ad visual |
| 3 | Text overlay | Stage 2 image + text-overlay prompt (with approved hook/CTA/font) | composed ad with headline, sub-text, CTA |
| 4 | Logo composite | Stage 3 image + logo file + compositing prompt | final deliverable |

Each stage is a **human-in-the-loop gate**: the user previews the output, then either **Approves** (image is locked and passed to the next stage), **Regenerates** (same prompt, new seed), or **Switches variant**. Nothing auto-advances.

The app also has an **Agent Suggestion layer** (§7): between stages the agent proposes hooks, CTAs, font, and aspect-ratio choices with reasoning — but a suggestion **only enters the pipeline after the user explicitly approves it**. Unapproved suggestions are never injected into any prompt.

---

## 2. Non-negotiable rules (read before writing any code)

1. **Prompts are IMMUTABLE.** All prompts in Appendix A were hand-refined over many iterations. Store them as raw template files (e.g. `/prompts/*.txt`) exactly as given. The ONLY permitted modifications are the whitelisted token substitutions in §6. Everything else — wording, line breaks, the `═══` dividers, the `--ar 4:5 --style raw --v 6` suffixes, casing, typos (e.g. "ULL CANVAS" in Stage 2 Prompt E, "FRON STAGE 2" style notes are not part of prompts) — stays untouched.
2. **Brand colors are LOCKED.** Every hex value, opacity, gradient direction, shadow spec, and layout percentage inside the prompts is part of the brand system and must never be exposed as editable, templated, or altered:
   - Gradient/brand: `#FFFFFF`, `#BDCFED`, `#A2C0E6`, `#1746A2`
   - Text: `#0F0F0F` (and its 85% opacity sub-text variant)
   - Accent: `#85AEFD`
   - Headline highlight gradient: `#86AFFE → #2653AB` (left-to-right linear)
   - CTA gradient: `#FF8A3D → #F26A1A` (135° diagonal), shadow `rgba(242, 106, 26, 0.25)` 0 8px 20px
3. **Brand kit reference (display it in the UI as a read-only "Brand Kit" panel, exactly as authored):**
   ```
   {
   Vertical gradient right side : #BDCFED TO #1746A2
   Vertical gradient left side : #FFFFFF TO #1746A2
   Horizontal gradient top left to top right : #FFFFFF TO #A2C0E6
   Horizontal gradient bottom left to bottom right : #1746A2 TO #1746A2
   }
   ```
4. **Text-placement is user-CHOSEN; styling is LOCKED.** In Stage 3 the user picks where the text block and CTA sit (left / right / center / top / bottom — §6.4). Everything *else* stays immutable: all sizing ratios (sub-text ~32% of headline, CTA ~38%), line heights (1.1x / 1.5x), spacing rules, colours, gradient highlight, and the pill CTA styling. The Stage-3 prompt also **preserves the provided image exactly** — it only renders text on top and must not regenerate, recompose, or change the aspect ratio. Templated values: content strings, font, and the two placement tokens.
5. **Chained images are mandatory.** Stage 2 must send the approved Stage 1 image with its prompt. Stage 3 must send the approved Stage 2 image. Stage 4 must send the approved Stage 3 image plus the user-uploaded logo. Never regenerate a downstream stage from scratch without the upstream approved image.
6. **No silent agent actions.** Agent suggestions render as cards with Approve / Edit / Dismiss. Only the approved (or approved-after-edit) value is substituted into the prompt template. Log every substitution in a visible "Prompt diff" panel so the user can audit exactly what changed vs. the canonical template.

---

## 3. Tech stack (recommended; substitute equivalents if needed)

- **Frontend:** React + Vite + Tailwind. Single-page app, left rail = pipeline stepper, center = canvas preview, right rail = controls (variant picker, dropdowns, agent suggestion cards).
- **Backend:** Node/Express (or Next.js API routes). Responsibilities: prompt template loading + token substitution, image-generation API calls, session state, file storage of every generated artifact (`/runs/<run-id>/stage-<n>/<variant>-<attempt>.png`).
- **Image generation provider:** abstract behind one interface `generateImage({ prompt, referenceImage?, width, height })` so the user can plug in Midjourney (via proxy), Gemini/Imagen, GPT-Image, Flux, or Stability. Stages 3–4 require an **image-editing-capable** model (image+text → image). Stage 4 may alternatively be done **deterministically with Sharp/Canvas** (preferred — see §5.4), since it is pure compositing.
- **State:** persist each run as JSON (chosen variants, approved tokens, prompt hashes, image paths) so a run can be resumed.

---

## 4. Pipeline state machine

```
IDLE
 └─> STAGE1_CONFIG (pick variant A/B; aspect ratio is fixed 16:9 here per prompt)
      └─> STAGE1_GENERATING ─> STAGE1_REVIEW ──approve──> STAGE2_CONFIG
                                   │ regenerate / switch variant ↺
 STAGE2_CONFIG (pick variant A–E; agent recommends; pick output aspect ratio §6.2)
      └─> STAGE2_GENERATING (sends Stage 1 approved image) ─> STAGE2_REVIEW ──approve──> STAGE3_CONFIG
 STAGE3_CONFIG (agent proposes hooks + CTAs + font; user approves/edits each token)
      └─> STAGE3_GENERATING (sends Stage 2 approved image) ─> STAGE3_REVIEW ──approve──> STAGE4_CONFIG
 STAGE4_CONFIG (user uploads logo PNG; preview placement)
      └─> STAGE4_GENERATING (sends Stage 3 approved image + logo) ─> STAGE4_REVIEW ──approve──> DONE
DONE: export final PNG at original resolution + run manifest JSON
```

Back-navigation is allowed at any time; going back invalidates downstream approvals (warn the user first).

---

## 5. Stage-by-stage behavior

### 5.1 Stage 1 — Gradient base
- Variant picker: **Prompt A** (diagonal sweep, white TL → royal blue BR) or **Prompt B** (inverted horizon, blue top → white bottom). Show a tiny CSS-gradient thumbnail preview of each so the user can choose visually before spending a generation.
- Prompts are 16:9 by design — keep as written. (The Stage 1 output is a texture source for Stage 2; the model handles the reframe.)
- The note "strict only use these only" applies: no other gradient prompts may ever be offered.

### 5.2 Stage 2 — Element on the Stage-1 background
Stage 2 adds **one subject** onto the approved Stage-1 background and merges the two seamlessly. The background is owned entirely by Stage 1 — Stage 2 never re-describes or recolours it.
- **One common prompt.** A single immutable blend template, `prompts/stage2_element_blend.txt` (`variants.STAGE2_BLEND_PROMPT`), does exactly one job: "keep the provided background exactly, add the `[SUBJECT]`, merge seamlessly for a premium feel." It contains **no colour palette, no gradient description, and no aspect-ratio text tokens.** `tokens.substitute_stage2` swaps the `[SUBJECT]` token for the chosen variant's subject; the canvas size is enforced via the API call's dimensions.
- **Variant picker = subjects only.** Each variant in `variants.STAGE2_VARIANTS` supplies just a `subject` string (the element — never the background), plus UI `title` / `desc` and a `category` (people, object, flatlay, architecture, scene) that groups the picker + the agent's element explorer (§7.1.1b). A–E are the originals re-expressed as subjects; F-onward extend the library.
- Must attach the **approved Stage 1 image** as the reference image, per the source doc: "TAKE IMAGE FROM STAGE 1 and send it with this prompt." The blend prompt explicitly tells the model to preserve that image as the final background.
- Aspect ratio is chosen at **Stage 1** and shown locked here (§6.2).

### 5.3 Stage 3 — Text overlay
- Source doc contains Prompt 1, Prompt 2, Prompt 3 — **they are byte-identical**. Store ONE canonical template (`/prompts/stage3_text_overlay.txt`) and use it for all three "slots"; expose a note in the UI ("3 source prompts, identical — canonical copy in use"). This is not a change to the prompts; it is deduplication of identical bytes.
- Must attach the **approved Stage 2 image**.
- Before generation, all four content tokens (§6.3) must be in `approved` state. The Generate button is disabled until then.
- Render a live **HTML/CSS mock preview** of the text layout (left 40% zone, gradient highlight, pill CTA) over the Stage 2 image thumbnail so the user can sanity-check the hook/CTA before spending a generation. The mock uses the locked spec values from the prompt.

### 5.4 Stage 4 — Logo
- User uploads a logo (PNG with transparency preferred; accept SVG and rasterize).
- **Preferred implementation: deterministic compositing with Sharp** (not an AI model), implementing the Stage 4 prompt's rules exactly: top-left placement, 4%-of-width margins, 20% width (25% if aspect ratio > 3:1 wide, 15% if taller than 1:2), proportional scaling, zero alteration of base pixels, output at identical dimensions. This guarantees the "every pixel … must remain identical" requirement, which generative models cannot.
- Also keep the AI path available (send Stage 3 image + logo + the verbatim Stage 4 prompt) behind a toggle labeled "AI compositor (not pixel-exact)". Default = deterministic.

---

## 6. Token substitution — the ONLY allowed prompt edits

Implement substitution as **exact-string replacement** against the canonical templates. Validate after substitution that no other byte changed (diff against template with tokens stripped). Show the diff in the audit panel.

### 6.1 Font token — Stage 3 only
- **The creative font is LOCKED to a single brand family: `Causten`.** The family files live in `Causten Font Family/*.otf` and are the canonical reference. Users may select any Causten *variation* but never a different family or free text.
- Exact string in template: `Artica Bold` (occurs in the TYPOGRAPHY block and the CTA block). This is now an internal substitution **anchor only** — it is never selectable in the UI and is always replaced with the chosen Causten variant before generation.
- UI: dropdown, default **Causten Bold**, grouped under the locked `Causten` family. Variations (Thin → Black, each upright + Oblique), defined in `variants.FONT_VARIANTS`:
  `Causten Thin` · `Causten Thin Oblique` · `Causten ExtraLight` · `Causten ExtraLight Oblique` · `Causten Light` · `Causten Light Oblique` · `Causten Regular` · `Causten Regular Oblique` · `Causten Medium` · `Causten Medium Oblique` · `Causten SemiBold` · `Causten SemiBold Oblique` · `Causten Bold` (default) · `Causten Bold Oblique` · `Causten ExtraBold` · `Causten ExtraBold Oblique` · `Causten Black` · `Causten Black Oblique`
- Replace **every** occurrence of the `Artica Bold` anchor with the selected Causten variation name. Nothing else in the typography block changes. The router rejects any font outside `variants.FONTS` with HTTP 400.

### 6.2 Aspect ratio tokens — Stage 2 and Stage 3
- UI: dropdown with presets:

| Label | Dimensions | AR token | Orientation word |
|---|---|---|---|
| Instagram Portrait (default) | 1080x1350px | 4:5 | Vertical |
| Square | 1080x1080px | 1:1 | Square |
| Story / Reel | 1080x1920px | 9:16 | Vertical |
| Landscape | 1920x1080px | 16:9 | Horizontal |

- Exact-string replacements in Stage 2 prompts (A–D contain these; E contains none — if E is chosen, pass dimensions via the API call only):
  - `1080x1350px (4:5 aspect ratio)` → `{W}x{H}px ({AR} aspect ratio)`
  - `--ar 4:5` → `--ar {AR}`
  - Leading word `Vertical` in the first line `Vertical social media post,` → `{ORIENTATION}` word from the table.
- Default = 4:5; when default is selected, templates are sent **completely untouched**.
- Stage 1 prompts stay 16:9 always (they say so internally; do not template them).

### 6.3 Content tokens — Stage 3 only

| Token | Exact string in template | Default value |
|---|---|---|
| HEADLINE | `Hire Experienced Virtual Legal Staff For Your Firm` | (same) |
| HIGHLIGHT_PHRASE | `Virtual Legal Staff` (occurs twice: "Gradient highlight on:" line and COLOR SPECIFICATIONS line) | (same) |
| SUBTEXT_LINE_1 | `Build your team with the best legal staff in the world.` | (same) |
| SUBTEXT_LINE_2 | `Choose from pre-vetted candidates — start in under 3 days.` | (same) |
| CTA_TEXT | `Book a Free Consultation` (the `  →` arrow notation around it stays untouched) | (same) |

- Constraints to enforce in UI validation: headline ≤ 9 words (prompt allows max 4 wrapped lines); HIGHLIGHT_PHRASE must be a contiguous substring of HEADLINE; sub-text lines ≤ 70 chars each; CTA ≤ 4 words.
- All styling around these strings (colors, gradient highlight spec, opacity, accent bars, button styling) is **never** editable.

### 6.4 Placement tokens — Stage 3 only
- Exact strings in template: `[TEXT_PLACEMENT]` and `[CTA_PLACEMENT]` (in the LAYOUT & POSITIONING block).
- UI: two segmented pickers. **Text placement** — `left` (default) · `right` · `center` · `top` · `bottom`. **CTA placement** — `bottom`/Below text (default) · `left` · `center` · `right` · `top`/Above text.
- The chosen key resolves to a descriptive phrase (`variants.TEXT_PLACEMENTS` / `CTA_PLACEMENTS`) that is substituted into the token; the live mock preview (`MockPreview`) mirrors the choice. Defaults reproduce the original left-aligned layout exactly (byte-identical when no placement is passed).
- The router rejects any placement key outside the allowed sets (HTTP 400).

---

## 7. Agent Suggestion layer (approval-gated)

A lightweight LLM "creative strategist" agent that proposes — never decides. Every suggestion is a card with **Approve · Edit · Dismiss**. State machine per suggestion: `proposed → (approved | edited→approved | dismissed)`. Only `approved` values flow into §6 tokens or variant pre-selection.

### 7.1 Where suggestions appear
1. **Before Stage 2 (concept choice):** agent asks the user 2–3 quick questions (campaign goal: lead-gen vs. brand; audience: solo attorneys vs. firm partners; emotional angle: aspiration vs. pain-point) then recommends ONE of variants A–E with a 2-sentence rationale, e.g. "Partners scrolling at night → D's burnout scene mirrors their reality; pair with an empathy hook." Recommendation only highlights the card — the user still clicks to choose.
   - **Element explorer (`suggest` kind `explore`).** A second action lets the agent "play" with the wider library: it surfaces a few less-obvious elements (biased toward objects/flatlays/architecture/scenes) plus one bold wildcard, each with a one-line creative reason, tuned to the answers. Curated + deterministic offline; when an OpenRouter key is configured the reasoning is rewritten by the model (`source: "agent+llm"`, best-effort — failures fall back to curated). It only proposes catalogue ids; nothing is generated until the user clicks a card.
2. **At Stage 2 (aspect ratio):** agent recommends an AR based on stated placement (Feed → 4:5, Story → 9:16, LinkedIn → 1:1/4:5) with rationale. Approval required before the dropdown auto-sets.
3. **Before Stage 3 (hooks + CTA):** the core feature. Agent generates, conditioned on the chosen Stage 2 concept:
   - **5 headline hooks** (each with its suggested HIGHLIGHT_PHRASE marked, word-count validated against §6.3)
   - **3 CTA options**
   - **2 sub-text pairs**
   - For concept D it should lean pain-point ("Still at the office at 11:47 PM?"-style); for B, social-proof/global-talent angles; for C/E, authority angles; for A, efficiency/warmth angles.
   - Each card shows a mini live preview (the §5.3 HTML mock) with that hook rendered.
4. **At Stage 3 (font):** agent may suggest a Causten *variation* (weight) from the locked family with one-line rationale; default remains Causten Bold unless approved. The family itself is never changeable.
5. **At every REVIEW step:** agent gives a short QA critique (e.g. "banding visible in upper gradient — consider regenerate"; "hands look malformed in Hex 2") as advisory text only. No auto-regeneration.

### 7.2 Hard rules for the agent
- Never modifies locked styling, colors, layout, or any non-whitelisted prompt text. Its outputs map ONLY to §6.3 tokens, variant choice, AR choice, font choice.
- If the user dismisses everything, defaults from §6.3 are used.
- All approved decisions are written to the run manifest: `{ token, source: "agent"|"user", original_suggestion, final_value, timestamp }`.

---

## 8. UI requirements summary

- **Pipeline stepper** (1 Gradient → 2 Photo → 3 Text → 4 Logo) with lock icons on approved stages.
- **Variant cards** with thumbnails (Stage 1: CSS gradient previews; Stage 2: concept descriptions).
- **Dropdowns:** Font (§6.1), Aspect Ratio (§6.2).
- **Agent suggestion cards** with Approve/Edit/Dismiss and live mock preview for hooks.
- **Prompt audit panel** (collapsible): shows the exact final prompt being sent, with substituted tokens highlighted, and a diff vs. canonical template.
- **Review screen:** large preview, Approve / Regenerate / Back, attempt history strip (every generation kept, user can approve any past attempt).
- **Export:** final PNG + `manifest.json` (all choices, prompt hashes, image lineage).

---

## 9. Acceptance criteria

1. Sending Stage 1–4 with all defaults produces API payloads whose prompt text is **byte-identical** to Appendix A (Stage 3 = the canonical copy; Stage 4 AI path = verbatim prompt). Write an automated test that hashes the canonical templates and asserts equality when no tokens are changed.
2. Changing only the font produces a prompt that differs from canonical **only** at `Artica Bold` occurrences. Same isolation property for AR and each content token (test each).
3. Stage N+1 generation request always contains the approved Stage N image.
4. No agent suggestion ever reaches a prompt without an explicit approve event in the manifest.
5. Deterministic Stage 4 output: base image pixels outside the logo bounding box are identical to Stage 3 input (write a pixel-diff test).
6. All brand hex values render correctly in the UI mock preview (visual spot-check list: #1746A2 backgrounds, #86AFFE→#2653AB headline highlight, #FF8A3D→#F26A1A CTA).

---

# Appendix A — CANONICAL PROMPTS (IMMUTABLE — store byte-for-byte)

Store each block below as its own file under `/prompts/`. The text between the BEGIN/END markers is the exact file content.

---

## A.1 — `/prompts/stage1_gradient_A.txt`

-----BEGIN PROMPT-----
Create a 16:9 aspect ratio immersive abstract background gradient. Use a smooth diagonal sweep flowing from the top-left to the bottom-right corner. Start with pure white #FFFFFF in the top-left, transitioning through soft light blue #BDCFED and #A2C0E6 in the middle, and ending with deep royal blue #1746A2 in the bottom-right. Add a subtle secondary diagonal blend from the top-right corner using #BDCFED fading into #1746A2. Soft, seamless blending with no harsh edges. Minimalist, cinematic, ultra-smooth gradient texture, high resolution, no noise, no text.
-----END PROMPT-----

## A.2 — `/prompts/stage1_gradient_B.txt`

-----BEGIN PROMPT-----
Create a 16:9 aspect ratio immersive abstract background gradient with an inverted horizon effect. The top of the image is deep royal blue #1746A2 flowing horizontally across, gradually transitioning downward into soft light blue #A2C0E6 and #BDCFED in the middle, and finishing with pure white #FFFFFF at the bottom. Subtle vertical blending on the left and right edges adds depth. Cinematic, atmospheric, ultra-smooth gradient with no harsh edges, no noise, no text, high resolution.
-----END PROMPT-----

> Source note (display in UI, not part of prompts): "note strict only use these only I am also specifying the brand kit colors for reference" — brand kit block is in §2.3.

## A.3 — `/prompts/stage2_photo_A.txt`
> Pipeline note: TAKE IMAGE FROM STAGE 1 and send it with this prompt.

-----BEGIN PROMPT-----
Vertical social media post, 1080x1350px (4:5 aspect ratio), single unified 
cinematic composition for a US legal-tech SaaS company that provides Virtual 
Assistants (VAs) to American law firms — ONE cohesive scene, NOT a layered 
composite, NOT a card collage.

TOP HALF (approximately top 45% of canvas): empty negative space with a smooth 
bilinear gradient — top-left white (#FFFFFF) flowing diagonally into deep 
royal blue (#1746A2) at bottom, with soft sky blue (#A2C0E6) at top-right. 
Buttery smooth gradient, no banding, no sharp color shifts. Plenty of clean 
negative space at the top for headline text overlay (the headline will be 
added later in design software).

TOP-RIGHT CORNER: ultra-faint PCB circuit traces in pale luminous blue, thin 
lines with tiny circular nodes, ghosted at 30% opacity, only in the upper-right 
quadrant, radially dissolving to nothing toward center.

BOTTOM HALF (approximately bottom 55% of canvas): a single candid 
documentary-style photograph occupying the lower portion of the canvas, 
showing a professional Virtual Assistant working remotely to support a US law 
firm.

SCENE CONTENT: a polished, professional Caucasian woman in her late 20s to 
mid-30s — fair skin, natural-toned makeup, light brown or dark blonde hair 
styled neatly (loose waves, a low ponytail, or a sleek shoulder-length cut), 
warm friendly expression, professional attire (a smart navy or cream blazer 
over a crisp white blouse). She is seated at a clean modern remote home-office 
workspace, wearing a sleek black headset, genuinely focused and smiling 
subtly while speaking on a client call. In front of her: a dual-monitor setup 
or laptop displaying legal case management software, calendars, and document 
interfaces (UI shown abstractly, not readable). Beside her: a neat stack of 
manila legal folders, a notepad with handwritten notes, a ceramic coffee cup 
— subtle legal cues that signal she's supporting attorneys remotely. 
Background: a softly blurred modern home office with warm natural light, a 
bookshelf with law books visible out of focus, a framed certificate or diploma 
on the wall hinting at legal training. Cinematic shallow depth of field with 
the VA in sharp focus.

COMPOSITION FOR VERTICAL FORMAT: frame her from chest-up to slightly-above-
head, centered or slightly right of center, with her workspace and props 
visible in the lower foreground. Her gaze should be directed slightly off-
camera (looking at her monitor), creating natural eyeline movement. Leave 
visual breathing room at the very bottom for a CTA button overlay.

MOOD: warm, productive, professional, efficient, trustworthy, "behind the 
scenes hero" energy. Think: modern remote-work culture meets legal 
professionalism. Approachable, intelligent, capable.

LIGHTING: warm soft natural daylight from a window on the right, soft fill 
light on the face flattering her fair complexion, golden hour interior glow, 
naturally lit photojournalism style, available light only, no flash. Skin 
tones look natural and healthy — not overly pale, not orange — true to life.

CRITICAL — SEAMLESS BLEND INTO GRADIENT (most important part for vertical 
layout):
The photograph does NOT have a visible rectangular border, frame, card, or 
edge. The bottom half of the canvas IS the photograph — it lives directly on 
the canvas. The photograph's TOP edge dissolves UPWARD into the blue gradient 
through a long, gradual alpha feather (250-300px tall soft fade) — the blue 
background atmospherically bleeds DOWNWARD into the top portion of the scene, 
so the ceiling, the upper wall, and the back of the room gradually become 
bathed in cool blue ambient light, then dissolve entirely into the gradient 
above. The transition is misty, atmospheric, cinematic — like the workspace is 
emerging from a blue sky-like haze above her. The LEFT and RIGHT edges of the 
photo also softly vignette into the gradient with a subtle 60-80px feather. 
Only the bottom edge of the photo touches the canvas edge cleanly.

COLOR CONTINUITY: the deep blue of the gradient picks up subtly in the cool 
shadows of the photo's upper background area, while the warm amber daylight 
remains untouched and fully saturated on her face, hands, and the focal 
workspace. Think: cinematic title-sequence atmosphere where two environments 
melt into one — sky above, workspace below.

NO HARD EDGES, NO CARD BORDERS, NO ROUNDED RECTANGLES, NO DROP SHADOWS — the 
photo is painted directly into the gradient like a double exposure or a 
cinematic dissolve transition. The horizon line between gradient and 
photograph should be invisible — pure atmospheric haze.

Style: premium B2B SaaS Instagram hero, modern legal services aesthetic, 
thumb-stopping social media quality, trustworthy, professional. Shot on Canon 
R5, 50mm f/1.8, photojournalism realism, available light, editorial campaign 
quality.

No text, no logos, no readable software UI, no readable documents, no 
nameplates, no captions, no card frames, no Polaroid borders, no harsh seams.

--ar 4:5 --style raw --v 6
-----END PROMPT-----

## A.4 — `/prompts/stage2_photo_B.txt`
> Pipeline note: TAKE IMAGE FROM STAGE 1 and send it with this prompt. Has a paired negative prompt (A.5).

-----BEGIN PROMPT-----
Vertical social media post, 1080x1350px (4:5 aspect ratio), single unified 
cinematic composition for Legal Soft — a US legal-tech SaaS company providing 
global Virtual Assistants to American law firms. Premium B2B SaaS aesthetic, 
minimal, sophisticated.

FULL CANVAS BACKGROUND: smooth bilinear gradient — top-left white (#FFFFFF) 
flowing diagonally into deep royal blue (#1746A2) at bottom-right, with soft 
sky blue (#A2C0E6) transitioning across the top. Buttery smooth, no banding.

TOP-RIGHT CORNER AREA: ultra-faint PCB circuit traces in pale luminous blue, 
ghosted at 25% opacity, weaving subtly behind the hexagonal cluster. Subtle 
blue dotted texture overlay at 8% opacity.

LOWER TWO-THIRDS OF CANVAS: generous empty negative space — clean gradient 
only, reserved for headline + CTA overlay.

TOP-RIGHT CORNER: a clean honeycomb cluster of exactly 3 candid documentary-
style photographs of professional Virtual Assistants, arranged in PROPER 
NON-OVERLAPPING honeycomb tessellation.

HEXAGON ARRANGEMENT — exactly 3 flat-top hexagons, ALL SAME SIZE (~260px 
wide each), arranged in a true honeycomb pattern with NO OVERLAP between any 
hexagons. Each hexagon occupies its own distinct position. Adjacent 
hexagons share clean edges (edges meet at hairline borders) but DO NOT 
overlap or sit on top of each other.

PRECISE LAYOUT GEOMETRY:

- HEX 1 (top, anchor): positioned in the top-right area of the canvas. Its 
right edge extends slightly beyond the canvas right edge (cropped, ~88% 
visible). Its top edge extends slightly beyond the canvas top (cropped, 
~88% visible). Fully anchored into the top-right corner.

- HEX 2 (left of Hex 1, same horizontal row): positioned to the LEFT of 
HEX 1 in the same horizontal row. Hex 2's right edge MEETS Hex 1's left 
edge at a shared vertical boundary — they sit SIDE BY SIDE on the same 
row, NOT overlapping, NOT stacked. Both hexagons are at the same vertical 
height. Hex 2 is fully visible within the canvas.

- HEX 3 (below, in the offset row): positioned in the row BELOW Hex 1 and 
Hex 2, offset by half a hexagon width (standard honeycomb tessellation 
offset). Hex 3 sits DIRECTLY BELOW the gap/junction between Hex 1 and Hex 
2 — meaning its upper-left edge meets Hex 2's lower-right edge, and its 
upper-right edge meets Hex 1's lower-left edge. Hex 3 is in a NEW ROW, not 
overlapping with the row above. Hex 3 is fully visible within the canvas.

CRITICAL GEOMETRY RULES:
- Three hexagons total, all identical size (~260px wide)
- Two distinct horizontal rows: Hex 1 + Hex 2 on top row, Hex 3 on bottom 
row offset
- NO hexagon overlaps another hexagon
- NO hexagon sits in front of or behind another hexagon
- Each hexagon occupies its own physical space in the honeycomb grid
- Adjacent edges meet cleanly at shared boundaries with hairline white 
borders separating them
- This is a flat 2D honeycomb tessellation, NOT a stacked or layered 
arrangement

Think of it like 3 tiles in a tile floor — they sit beside each other on 
the same plane, edges touching, never one tile on top of another.

HEXAGON CONTENT:

HEX 1 (top-right, anchor): a confident professional Filipina woman in her 
early 30s, warm medium-tan skin, dark brown hair, wearing a navy blazer 
over a white blouse, sleek black headset, genuinely engaged smile while on 
a client call, looking slightly off-camera. Soft natural lighting.

HEX 2 (top-left of pair): a professional Filipino or Latino man in his 
early 30s, short dark hair, light blue shirt, wearing a black headset, 
focused candid expression looking at his monitor. Warm interior lighting.

HEX 3 (bottom, offset): a professional Latina woman in her late 20s, wavy 
dark brown hair, cream or light blazer, focused on her laptop, soft natural 
smile, taking notes. Soft daylight.

ALL FACES VISIBLE, sharp focus, candid documentary style, warm natural 
lighting, professional but human.

PROGRESSIVE BLEND — applied at OUTER edges of the cluster only:
- HEX 1 (corner anchor): 100% opacity, fully crisp.
- HEX 2 (left side of cluster): 90% opacity, with its LEFT and LOWER-LEFT 
outer edges (the edges facing the gradient negative space, NOT the edge 
shared with Hex 1) dissolving softly into the gradient through a ~60px 
alpha feather.
- HEX 3 (bottom of cluster): 80% opacity, with its LOWER and LOWER-LEFT 
outer edges (facing the gradient, NOT the edges shared with Hex 1 and Hex 
2) dissolving into the gradient through a ~80px alpha feather.

The INNER edges where hexagons meet each other remain crisp and defined — 
hairline white borders show the tessellation clearly. Only the OUTER edges 
(facing the empty gradient) feather and dissolve.

HEXAGON STYLING:
- Hairline 1-2px white borders along every hexagon edge
- The shared internal edges between adjacent hexagons remain crisp
- Outer edges fade with the opacity gradient described above
- Very soft pale-blue outer glow around the entire cluster (~25px radius, 
8% opacity)
- Photos inside hexagons are FULLY OPAQUE, FULLY SATURATED, crisp — NO 
blue tint on skin, NO color overlay on photo content

BRAND DETAIL: a single small circular Philippines flag chip (~24px) 
floating just outside the lower-right edge of HEX 1, thin white border, 
fully opaque.

NO OVERLAPPING HEXAGONS, NO STACKED HEXAGONS, NO HEXAGONS LAYERED ON TOP OF 
EACH OTHER. Each hex is its own discrete tile in a flat honeycomb pattern. 
NO connecting lines, NO particles, NO decorative elements, NO text.

MOOD: premium, confident, atmospheric. Compact corner accent that signals 
"vetted global talent" without dominating the canvas.

Style: premium B2B SaaS Instagram hero, Linear/Stripe/Vercel-level design 
restraint, modern legal-tech, editorial campaign quality. Shot on Canon R5, 
50mm f/1.8, photojournalism realism, available light.

No text, no logos, no readable software UI, no readable documents, no 
captions, no decorative flourishes.

--ar 4:5 --style raw --v 6
-----END PROMPT-----

## A.5 — `/prompts/stage2_photo_B_negative.txt`

-----BEGIN PROMPT-----
overlapping hexagons, hexagons on top of each other, stacked hexagons, 
layered hexagons, hexagons sitting in front of other hexagons, hexagons 
behind other hexagons, hex overlapping another hex, photos overlapping, 
photos covering parts of other photos, faces partially hidden by another 
hexagon, hexagons clipping into each other, 3D stacked arrangement, 
floating hexagons in front of each other, depth layering, hexagons in 
center of canvas, hexagons at bottom, hexagons on left side, mixed 
hexagon sizes, irregular hexagon sizes, more than 3 hexagons, gaps 
between adjacent hexagons in the cluster, octagons, pentagons, square 
photos, circular photos, thick heavy hexagon borders, connecting lines, 
network diagram lines, glowing dots, particle effects, sparkles, heavy 
drop shadows, blue tint on faces, washed out photos, color overlay on 
faces, oversized flag, flag covering face, cartoon, illustration, anime, 
stiff stock-photo poses, fake smiles, readable text, gibberish text, 
plastic skin, oversaturated, cluttered composition
-----END PROMPT-----

## A.6 — `/prompts/stage2_photo_C.txt`
> Pipeline note: TAKE IMAGE FROM STAGE 1 and send it with this prompt.

-----BEGIN PROMPT-----
Vertical social media post, 1080x1350px (4:5 aspect ratio), single unified 
cinematic composition for Legal Soft — a US legal-tech SaaS company providing 
trained Virtual Assistants to American law firms. Premium B2B SaaS aesthetic, 
editorial campaign quality, BRAND-FORWARD design.

FULL CANVAS BACKGROUND: smooth bilinear brand gradient — top-left pure white 
(#FFFFFF) flowing diagonally into deep royal blue (#1746A2) at the bottom-
right, with soft sky blue (#A2C0E6) transitioning across the top-right. 
Buttery smooth, no banding, no sharp color shifts. The gradient is BRAND-
PRESENT and confident — this is Legal Soft's signature visual identity, 
strong and elegantly executed. Saturation matches a premium brand campaign.

GRADIENT BEHAVIOR:
- Top-left corner: clean white (#FFFFFF) — strong headline contrast zone
- Upper-right area: soft brand sky blue (#A2C0E6)
- Lower-left to bottom: deep brand royal blue (#1746A2) — full saturation
- Bottom-right corner: anchored in deep brand royal blue (#1746A2)
- Transitions are smooth and gradual but the brand colors hit their FULL 
intended saturation in their corner zones — no muting, no desaturation

TOP-RIGHT CORNER: faint PCB circuit traces in pale luminous blue, ghosted 
at 25-30% opacity, only in the upper-right corner, radially dissolving 
toward center. Subtle blue dotted texture overlay at 8% opacity adding 
depth to the brand atmosphere.

LEFT 45-50% OF CANVAS: generous negative space within the gradient — the 
white-to-light-blue zone — reserved for headline text + CTA overlay (added 
later in design software).

RIGHT 50-55% OF CANVAS: a single FULL-FIGURE candid documentary-style 
photograph of a professional Caucasian male Virtual Assistant attending a 
client call, occupying the right portion of the canvas from top to bottom.

SUBJECT — full-figure male VA:
A polished professional Caucasian man in his early 30s to early 40s, 
photographed in a NEAR-FULL-BODY or three-quarter-body composition (visible 
from head to mid-thigh or below knee). He is shown at his workstation in a 
genuine candid moment during a professional client call.

APPEARANCE:
- Fair to medium-fair skin with natural healthy tone (NOT overly pale, NOT 
orange-tanned)
- Short to medium-length neatly styled brown or dark blonde hair — classic 
side part, modern textured crop, or a polished slicked-back style
- Clean-shaven OR neatly trimmed light stubble / short professional beard
- Sharp jawline, intelligent attentive expression
- Confident but approachable presence — focused listening face or subtle 
engaged smile, NOT a wide forced grin

ATTIRE — FULL FORMAL BUSINESS PROFESSIONAL:
- A tailored two-piece business suit in navy, charcoal, or dark grey — 
matching jacket and trousers, properly fitted to his frame
- A crisp white, pale blue, or subtle striped formal dress shirt underneath
- A professional silk tie in a complementary color (navy, burgundy, deep 
grey, or subtle pattern) — knotted neatly with a clean dimple, sitting 
properly at the collar
- Polished black or dark brown leather oxford or derby dress shoes (visible 
in the lower portion of the frame)
- Subtle professional accessories: a quality wristwatch (leather strap or 
stainless steel), perhaps a discreet tie bar, possibly a folded pocket 
square (optional, understated)
- The full formal outfit must be clearly visible — this is NOT business-
casual, this is full corporate attorney-office professional attire

POSE AND ACTION:
- He is standing or seated at his desk in a poised, confident professional 
posture — full body visible, NOT just head-and-shoulders
- Wearing a sleek black or grey professional headset over his ear
- One hand may be lightly adjusting the headset's mic, holding a leather 
portfolio / document folder, OR gesturing naturally mid-conversation
- Could be standing beside his desk while on the call (more dynamic, full-
body visible, weight on one foot, relaxed-professional stance) OR sitting 
upright at his workstation with jacket fully on and properly buttoned
- Looking slightly off-camera (toward his monitor or thoughtfully into the 
distance) — candid mid-conversation moment, NOT posing for camera

ENVIRONMENT:
- Modern professional remote workspace — clean, organized, premium home-
office or executive co-working space
- Softly blurred background featuring: a sleek modern desk, monitors 
showing abstract software interfaces (NOT readable), a neatly arranged 
leather portfolio, a quality pen, a coffee cup or glass of water, a small 
plant, a framed certificate or law degree on the wall, a bookshelf with 
law books out of focus
- Warm natural daylight from a window on the right side, soft fill 
lighting, golden interior glow
- Shallow depth of field with him in sharp focus, background gently blurred

COMPOSITION FOR VERTICAL FORMAT:
- He is positioned in the right portion of the canvas, full figure visible 
from head down to at least mid-thigh
- Body fills the vertical space — head near the top quarter of canvas, 
torso in center, legs/lower body visible in the bottom quarter
- Slight 3/4 body turn toward the viewer's left, away from the canvas 
right edge — creates natural diagonal energy
- Negative gradient space on the LEFT of his figure for headline placement

LIGHTING: warm natural daylight from the right side (window light), soft 
fill on the face flattering his complexion, golden hour interior glow, 
naturally lit photojournalism style, available light only, no flash. Skin 
tones natural and healthy — neither washed out nor orange-tanned.

CRITICAL — SEAMLESS BLEND INTO BRAND GRADIENT:
The photograph does NOT have a visible rectangular border, frame, or card 
edge. The right portion of the canvas IS the photograph. His figure stands 
crisp and clearly defined within the scene, but the LEFT edge of the 
photograph (where his workspace background meets the open brand canvas) 
dissolves softly into the brand gradient through a long gradual alpha 
feather (~300-350px wide).

The workspace background — wall, blurred furniture, ambient room — gently 
fades into the brand blue gradient on the left side. The brand blue 
atmospherically bleeds INTO the cool shadows on the left side of his 
workspace, while the warm daylight on his face, suit, and focal areas 
remains fully saturated.

The TOP edge of the photo also softly vignettes into the lighter sky-blue 
gradient with a subtle 80-100px feather. The BOTTOM edge softly vignettes 
into the deep brand royal blue with a similar feather. Only the right edge 
touches the canvas edge cleanly.

The effect: his professional figure emerges seamlessly from the Legal Soft 
brand atmosphere — he IS Legal Soft, not pasted onto Legal Soft.

COLOR HARMONY:
- The deep brand royal blue at the bottom is REINFORCED by his dark navy/
charcoal suit — the formal tailoring picks up and echoes the brand color
- His tie can subtly echo brand tones (navy especially complements the 
gradient beautifully)
- The warm interior daylight in the photo creates premium contrast against 
the cool brand gradient
- White elements (his dress shirt, ambient highlights) connect visually 
with the white headline space on the left
- Result: cohesive brand-photo harmony where the figure feels integrated 
into Legal Soft's visual world, NOT placed on top of it

HARD RULE: his face, skin tone, and the focal areas of his body retain 
WARM natural lighting — golden, healthy, dimensional. The blue brand 
gradient only affects the AMBIENT WORKSPACE BACKGROUND on the left side, 
NEVER the subject himself. Warm subject against cool brand background = 
the correct premium contrast.

NO HARD EDGES, NO CARD BORDERS, NO ROUNDED RECTANGLES, NO DROP SHADOWS, 
NO PHOTO FRAME — the photograph is painted directly into the brand 
gradient like a cinematic dissolve.

MOOD: confident, capable, brand-forward, executive-trustworthy. He is 
polished and authoritative, emerging from Legal Soft's signature blue 
atmosphere — premium corporate professionalism inside a recognizable brand 
world. The kind of professional an attorney would proudly introduce as 
part of their firm's team. NOT a fashion model, NOT a stock-photo 
businessman cliche.

Style: premium B2B SaaS Instagram hero, brand-forward legal services 
campaign, editorial corporate aesthetic with strong brand identity, GQ-
business-section meets enterprise SaaS. Shot on Canon R5, 50mm f/1.4, 
natural light photojournalism, premium commercial photography quality.

No text, no logos, no readable software UI, no readable documents, no 
nameplates, no captions, no harsh seams.

--ar 4:5 --style raw --v 6
-----END PROMPT-----

## A.7 — `/prompts/stage2_photo_D.txt`
> Pipeline note: TAKE IMAGE FROM STAGE 1 and send it with this prompt.

-----BEGIN PROMPT-----
Vertical social media post, 1080x1350px (4:5 aspect ratio), single unified 
cinematic composition for Legal Soft — a US legal-tech SaaS company providing 
trained Virtual Assistants to American law firms. Premium B2B SaaS aesthetic, 
editorial campaign quality, ATTENTION-STOPPING conceptual visual storytelling.

FULL CANVAS BACKGROUND: smooth bilinear brand gradient — top-left pure white 
(#FFFFFF) flowing diagonally into deep royal blue (#1746A2) at the bottom-
right, with soft sky blue (#A2C0E6) transitioning across the top-right. 
Buttery smooth, Legal Soft's signature brand identity, confident and strong.

TOP-RIGHT CORNER: faint PCB circuit traces in pale luminous blue at 25-30% 
opacity, radially dissolving toward center. Subtle blue dotted texture at 
8% opacity.

LEFT 45-50% OF CANVAS: generous negative space within the gradient for 
headline + CTA overlay.

RIGHT 50-55% OF CANVAS: a single dramatic, cinematic photograph featuring 
the HERO CONCEPT below.

═══════════════════════════════════════════════════════════════
THE HERO CONCEPT — "THE PARTNER'S CORNER OFFICE AT 11:47 PM"
═══════════════════════════════════════════════════════════════

The scene: an empty, prestigious senior partner's corner office in a top-
tier American law firm — photographed at NIGHT, lit only by a single 
warm desk lamp casting a golden pool of light onto a desk overflowing with 
work. The OWNER OF THIS OFFICE IS NOT IN FRAME — but their presence is 
felt through the chaos and ambition the room holds.

THE STORY THE IMAGE TELLS WITHOUT WORDS:
"This is what 'success' looks like at 11:47 PM. The trial brief is half-
written. The phone has 23 missed calls. The coffee went cold three hours 
ago. The view of Manhattan is breathtaking — but nobody's looking at it. 
You built this firm. Now it's eating you alive."

Every law firm partner who scrolls past this image will FEEL it in their 
chest. It's their life on a screen.

═══════════════════════════════════════════════════════════════

SCENE COMPOSITION — what's in the frame:

THE DESK (foreground, dominant):
- A heavy executive mahogany or walnut desk, polished, partially visible 
in the lower foreground of the frame
- A single brass-shaded green banker's lamp (classic legal library style) 
glowing warm gold — the ONLY light source in the room, creating a dramatic 
pool of warm light
- Scattered across the desk: stacks of legal documents and case files in 
manila folders, some open with visible pages (text not readable), a 
yellow legal pad with handwritten notes, multiple highlighters and a 
quality fountain pen left mid-thought
- An open leather portfolio with documents spilling out
- A half-full crystal whiskey glass or a cold coffee cup with cream 
swirls settled (untouched for hours)
- A premium leather-bound legal book left open face-down
- A reading glasses pair set down on top of the documents
- A modern laptop open and glowing softly, screen showing abstract 
software interfaces (NOT readable, just the impression of active work)
- A modern smartphone face-up on the desk with a glowing notification 
screen visible — multiple missed call indicators softly glowing (NOT 
readable text, just the impression of accumulated notifications)

THE OFFICE ENVIRONMENT (background, atmospheric):
- A floor-to-ceiling window or wall of windows on the right side of the 
office, revealing a stunning NIGHTTIME CITYSCAPE view of an American 
financial district (Manhattan-style skyline, Chicago loop, or generic 
American big-city skyscrapers) glittering with thousands of office 
lights and city traffic far below
- The city lights outside are warm golden and cool blue, scattered like 
stars across the dark sky
- A tufted oxblood or burgundy leather executive chair behind the desk, 
positioned slightly askew — pushed back as if the owner stepped away 
momentarily
- A wall of dark mahogany bookshelves filled with leather-bound law 
books behind the desk, partially visible in shadow
- A framed law degree and bar admission certificate on the wall, just 
visible in the ambient glow
- A subtle American flag on a brass stand in the corner shadow (NOT 
prominent, just an authentic detail)
- The rest of the office melts into dramatic shadows — deep navy and 
charcoal shadows fill the corners

LIGHTING — THIS IS WHERE THE MAGIC HAPPENS:
- ONE warm golden pool of desk lamp light illuminating the work in the 
foreground — creating a dramatic chiaroscuro effect
- The cityscape outside provides a secondary cool blue light source 
through the windows, creating premium warm-vs-cool contrast
- Deep shadows envelop the rest of the office
- Subtle warm amber glow from the laptop screen and phone screen adds 
ambient highlights
- Time of day: late night, somewhere between 11 PM and 1 AM — the city 
is still alive, but the office is in solitude

PHOTOGRAPHY ANGLE:
- Cinematic medium-wide angle, slightly low perspective, looking across 
the desk surface from the chair side toward the window
- Shallow-to-medium depth of field — the desk work is sharply focused 
in the foreground, the cityscape softly bokeh'd in the background
- The empty executive chair is visible but slightly out of focus, 
emphasizing the ABSENCE

═══════════════════════════════════════════════════════════════

ATTENTION-GRABBING DETAILS THAT MAKE LAW FIRM PARTNERS STOP SCROLLING:

1. THE EMPTY CHAIR — psychologically powerful. They see themselves NOT in 
the chair, working from somewhere else, having missed dinner again.

2. THE GLOWING PHONE WITH MISSED CALLS — every partner knows that screen. 
The dread it creates is universal. Their pulse will quicken.

3. THE COLD UNTOUCHED COFFEE — visual shorthand for "you've been at this 
for hours and you can't even pause to drink." Every partner has lived this.

4. THE BREATHTAKING VIEW NOBODY'S LOOKING AT — the cruel irony of 
"success." They built this to enjoy the view, and now they never see it.

5. THE OPEN BUT ABANDONED WORK — implies they walked away in the middle 
of something important. Could be a family emergency, an exhausted 
collapse, or simply giving up for the night.

6. THE GOLDEN POOL OF LAMP LIGHT — cinematic and seductive, like a 
prestige drama film still (Suits, The Good Wife, Better Call Saul).

═══════════════════════════════════════════════════════════════

CRITICAL — SEAMLESS BLEND INTO BRAND GRADIENT:

The photograph does NOT have a visible rectangular border, frame, or card 
edge. The right portion of the canvas IS the photograph. The scene is 
crisp in its focal areas, but the LEFT edge of the photograph (where the 
office's dark interior meets the open brand canvas) dissolves softly into 
the brand gradient through a long gradual alpha feather (~300-350px wide).

The dark office shadows on the left side fade into the deep brand royal 
blue (#1746A2) — they ARE the deep blue, atmospherically. The cityscape 
window light bridges with the brand sky-blue tones in the upper area. 
The warm golden lamp pool remains fully saturated and dominant in the 
focal area.

TOP edge softly vignettes into the lighter brand sky-blue gradient with 
80-100px feather. BOTTOM edge softly vignettes into the deep brand royal 
blue with similar feather. Only the right edge touches the canvas cleanly.

The effect: the partner's office emerges seamlessly from Legal Soft's 
brand atmosphere — the visual story IS Legal Soft's world.

COLOR HARMONY:
- The warm golden lamp light + city lights create premium warm contrast 
against the cool brand blue gradient
- The deep navy/charcoal office shadows harmonize with the brand royal 
blue
- The dark mahogany wood tones add richness without competing with the 
brand palette
- Result: cinematic warm-vs-cool that mirrors the EMOTIONAL warm-vs-cool 
of the story (the warmth of ambition vs. the cold reality of burnout)

HARD RULE: the focal warm pool of light on the desk remains fully 
saturated warm amber. The blue brand gradient only affects the AMBIENT 
SHADOWS and SKY background. Warm focal storytelling against cool brand 
atmosphere = the correct premium contrast.

NO HARD EDGES, NO CARD BORDERS, NO PHOTO FRAME — painted directly into 
the brand gradient like a cinematic film still.

═══════════════════════════════════════════════════════════════

MOOD MESSAGE: "We see you. The 11 PM grind, the missed dinner, the 
swelling caseload. Legal Soft is the partner who shows up when you're 
stretched thin." This isn't an ad about staffing — it's an ad about 
RECOGNITION of their reality.

Style: cinematic prestige-drama still (think Better Call Saul / Industry 
/ Succession color grading), premium architectural editorial photography, 
GQ-meets-Bloomberg-Law. Shot on Cinema Camera, 35mm lens, ambient light 
only, dramatic chiaroscuro, deep cinematic shadows, golden lamp warmth 
against cool nightscape.

No text in the image, no logos, no readable software UI, no readable 
documents, no readable phone notifications, no harsh seams, no people in 
frame, no harsh photo border.

--ar 4:5 --style raw --v 6
-----END PROMPT-----

## A.8 — `/prompts/stage2_photo_E.txt`
> Pipeline note: TAKE IMAGE FROM STAGE 1 and send it with this prompt. Source begins with "ULL CANVAS" (sic) — preserve as-is. Contains no `--ar` suffix and no dimension line; pass canvas dimensions via API parameters only.

-----BEGIN PROMPT-----
ULL CANVAS BACKGROUND: smooth bilinear brand gradient — top-left pure white (#FFFFFF) flowing diagonally into deep royal blue (#1746A2) at the bottom- right, with soft sky blue (#A2C0E6) transitioning across the top-right. Buttery smooth, no banding, no sharp color shifts. Legal Soft's signature visual identity, strong and confident. GRADIENT BEHAVIOR: - Top-left corner: clean white (#FFFFFF) — headline contrast zone - Upper-right area: soft brand sky blue (#A2C0E6) - Lower-left to bottom: deep brand royal blue (#1746A2) — full saturation - Bottom-right corner: anchored in deep brand royal blue (#1746A2) - Brand colors hit FULL intended saturation in their corner zones TOP-RIGHT CORNER: faint PCB circuit traces in pale luminous blue, ghosted at 25-30% opacity, only in the upper-right corner, radially dissolving toward center. Subtle blue dotted texture overlay at 8% opacity. LEFT 45-50% OF CANVAS: generous negative space within the gradient — the white-to-light-blue zone — reserved for headline + CTA overlay. RIGHT 50-55% OF CANVAS: a single dramatic architectural photograph of iconic US judiciary / federal courthouse architecture, occupying the right portion of the canvas from top to bottom.

SUBJECT — PRESTIGIOUS BIG LAW OFFICE TOWER:

Featured building: an iconic American Big Law firm headquarters — a 
sophisticated modern-classical office tower in a major US legal city 
(Manhattan, Chicago Loop, DC, San Francisco, LA). The building reads as 
unmistakably PRESTIGIOUS LAW FIRM, not generic corporate.

ARCHITECTURAL ELEMENTS:
- A polished granite or limestone facade with classical proportions
- Tall vertical bays of bronze-framed windows reflecting golden hour light
- A grand street-level entrance with revolving doors and a covered portico
- Possibly a subtle stone-carved firm name on the facade (not readable, 
just the impression of carved lettering)
- Manicured small landscaping at the entry
- The building rises 15-30 stories, photographed from a heroic upward 
angle showing the soaring vertical lines
- Modern but timeless — like Skidmore Owings & Merrill or Pei Cobb Freed 
architecture, NOT a generic glass box

PHOTOGRAPHY: heroic upward angle, golden hour light raking across the 
facade, classical American urban context.
-----END PROMPT-----

## A.9 — `/prompts/stage3_text_overlay.txt` (canonical — source "prompt 1"; source prompts 2 and 3 are byte-identical to this)
> Pipeline note: TAKE RESULT image FROM STAGE 2 and send it with this prompt. Token substitution per §6.1–§6.3 ONLY (font name, headline, highlight phrase, sub-text lines, CTA text). Every color, gradient, shadow, layout %, sizing ratio, and styling rule below is LOCKED.

-----BEGIN PROMPT-----
Add advertisement text overlay to the LEFT side of the provided image. Keep the original image untouched on the right side. Follow these exact specifications:

═══════════════════════════════════
LAYOUT & POSITIONING
═══════════════════════════════════
- Text block placement: LEFT side of the image (occupy ~40% of total width — DO NOT exceed)
- Vertical alignment: Centered
- Left margin: ~6% from the left edge
- Right boundary of text: stops at ~42% of total image width
- Top & bottom padding: ~8% each
- Right side: original image fully visible, no overlap

═══════════════════════════════════
TEXT CONTENT
═══════════════════════════════════
1. HEADLINE (bold, punchy):
   "Hire Experienced Virtual Legal Staff For Your Firm"
   - Gradient highlight on: "Virtual Legal Staff"
   - Other words in #0F0F0F
   - Max 4 lines, natural wrapping, no clipping

2. SUPPORTING SUB-TEXT (styled as TWO short value statements, NOT a paragraph):
   Break the message into 2 distinct lines with subtle visual rhythm:
   
   Line 1: "Build your team with the best legal staff in the world."
   Line 2: "Choose from pre-vetted candidates — start in under 3 days."
   
   - Each line on its own with slightly more line-gap between them than within
   - Color: #0F0F0F at ~85% opacity (slightly softer than headline, gives hierarchy)
   - Line height: 1.5x
   - Optional: add a small thin accent bar/dot (in #85AEFD) before each line, OR a subtle minimalist check icon — pick whichever feels cleaner. Icons must be SMALL and refined, not chunky.
   - DO NOT render as a flat paragraph block. Treat it like two crisp value props.

3. CTA BUTTON TEXT:
   "Book a Free Consultation  →"   (include a small right-arrow at the end inside the button)

═══════════════════════════════════
TYPOGRAPHY
═══════════════════════════════════
- Font family: "Artica Bold" for headline and CTA
- Sub-text: "Artica Bold" but slightly lighter visual weight via opacity (85%)
- Headline size: moderate — longest line fits within left 40% with margin
- Sub-text size: ~32% of headline size
- CTA text size: ~38% of headline size
- Line height (headline): 1.1x
- Line height (sub-text): 1.5x
- Letter spacing: normal/tight

═══════════════════════════════════
CTA BUTTON — PREMIUM SAAS STYLING (CRITICAL)
═══════════════════════════════════
- Shape: Pill-shaped, fully rounded corners
- Background: Vibrant orange GRADIENT (not flat color)
   → Start: #FF8A3D (lighter orange, top-left)
   → End:   #F26A1A (deeper orange, bottom-right)
   → Direction: diagonal (135°)
- Subtle inner highlight on top edge (very faint white at low opacity) to add dimension
- Soft drop shadow underneath:
   → Color: orange-tinted shadow (rgba(242, 106, 26, 0.25))
   → Offset: 0px 8px 20px blur
   → Creates a "floating button" premium feel, NOT a harsh black shadow
- Text inside: White (#FFFFFF), Artica Bold, with a small right-arrow "→" after the text (same color)
- Padding inside button: ~22px vertical, ~32px horizontal (generous, but compact)
- Button width: hugs the content (text + arrow + padding) — NOT stretched
- Slight icon/text gap: small space between "Consultation" and "→"
- The button should feel CLICKABLE, ELEVATED, and PREMIUM — like a Stripe, Linear, or Notion CTA button

═══════════════════════════════════
COLOR SPECIFICATIONS
═══════════════════════════════════
- Primary text: #0F0F0F
- Sub-text: #0F0F0F at 85% opacity
- Accent (small bars/dots before sub-text lines, if used): #85AEFD
- Highlighted headline words ("Virtual Legal Staff"): linear gradient left-to-right
   → Start: #86AFFE
   → End:   #2653AB
- CTA button: orange gradient #FF8A3D → #F26A1A (135°)
- CTA shadow: rgba(242, 106, 26, 0.25), soft, 0 8px 20px

═══════════════════════════════════
SPACING BETWEEN ELEMENTS
═══════════════════════════════════
- Headline → Sub-text: comfortable gap (~1.5x sub-text size)
- Sub-text → CTA: generous gap (~2.5x sub-text size) — CTA must feel like its own confident element, not stuck to the paragraph
- Between sub-text lines: slightly more spacing than default line-height for that "premium breathing room" feel

═══════════════════════════════════
STYLE RULES
═══════════════════════════════════
- Premium SaaS landing-page aesthetic — think Stripe, Linear, Webflow
- Sub-text must read as crisp value props, NOT a wall of paragraph text
- CTA must feel elevated, gradient-rich, with soft glow — not flat
- No element clipped, overlapping, or touching the right image
- No clutter, no decorative noise

═══════════════════════════════════
DELIVERABLE
═══════════════════════════════════
- Final composed advertisement with refined text overlay on LEFT only
- Right-side image untouched
- Original resolution maintained
-----END PROMPT-----

## A.10 — `/prompts/stage4_logo_composite.txt`
> Pipeline note: Take result of Stage 3 image and send it with this prompt (plus the logo image) to get the final result. Preferred implementation is the deterministic Sharp compositor (§5.4) which implements these rules exactly; this verbatim prompt is used only when the "AI compositor" toggle is on.

-----BEGIN PROMPT-----
You are an image compositing tool. I will provide a base image and a logo image. Overlay the logo onto the base image following these exact rules.
Placement:

Position the logo in the top-left corner.
Leave a margin from the top and left edges equal to 4% of the base image's width.

Sizing (the sweet spot — do not go smaller):

The logo's width must equal 20% of the base image's width.
Scale the logo's height proportionally to preserve its original aspect ratio. Do not stretch or distort.
If the logo is unusually wide (aspect ratio wider than 3:1, like a horizontal wordmark), size it to 25% of the base image's width instead so the text stays readable.
If the logo is unusually tall (aspect ratio taller than 1:2), size it to 15% of the base image's width so it doesn't run down the side.
Never render the logo smaller than these values. If in doubt, err on the larger side.

Preservation (critical):

Do not alter the base image: no cropping, recoloring, filtering, regenerating, upscaling, compressing, or restyling. Every pixel of the base image outside the logo area must remain identical to the original.
Do not alter the logo's colors, proportions, or transparency. Preserve transparent backgrounds.
Output the final image at the exact same dimensions and resolution as the original base image.

Output: Return only the final composited image. No description, no caption, no added borders.
-----END PROMPT-----

---

# Appendix B — Quick-start instruction for Opus

Build in this order:
1. Scaffold repo (`/prompts`, `/server`, `/web`, `/runs`). Write all Appendix A prompts to disk first and commit; add the hash-equality test (§9.1) immediately so any accidental edit fails CI.
2. Implement the provider-agnostic `generateImage` interface with one working provider + a mock provider for dev.
3. Build the pipeline state machine + run persistence.
4. Build the UI shell: stepper, variant cards, review screen with Approve/Regenerate.
5. Implement §6 token substitution with the isolation tests (§9.2).
6. Implement the Stage 3 live HTML mock preview.
7. Implement the agent suggestion layer (§7) with approval gating and manifest logging.
8. Implement deterministic Stage 4 compositor (Sharp) + pixel-diff test, with AI-path toggle.
9. Export + manifest.