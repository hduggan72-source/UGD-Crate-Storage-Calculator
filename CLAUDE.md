# CLAUDE.md — AquaCell Design Verification Dashboard / Crate Calculator

This file is read by Claude Code at the start of every session in this repository.
It defines what you are allowed to touch, what you must never touch, and the process
you must follow before any commit or PR. These rules are not suggestions — they exist
because parts of this codebase are live production tools that real clients and internal
estimators depend on daily.

---

## 1. Project Overview

**Purpose:** Internal Flask/Python web application for AquaCell underground stormwater
detention system sizing, BOM generation, quoting, technical submittals, and engineering
verification. Used by internal Wavin estimators (full tool) and, on a separate branch,
by external engineers/designers (stripped-down client-facing BETA).

**Owner:** James (Heath Duggan) — Wavin sales estimator, AquaCell specialist. James makes
all product/feature/modeling decisions. You implement; you do not decide engineering
methodology or business scope on your own. If a modeling or UX decision is ambiguous,
STOP and ask — do not silently pick an interpretation.

**Repository:** `hduggan72-source/UGD-Crate-Storage-Calculator`
**Production branch:** `Pricing_Engine_v1`
**Deployment:** Render, live testing after each change

---

## 2. Branch Rules — READ THIS FIRST

- **You may never push directly to `Pricing_Engine_v1`.** This is the live production
  branch. All work happens on feature branches.
- **Workflow for every change:** create a feature branch → make changes → run the full
  audit sequence (Section 5) → open a Pull Request → STOP and wait for James to review
  and merge. You do not merge your own PRs.
- **The BETA/client-facing branch** (name TBD by James — confirm exact branch name at
  session start, do not assume) is the active development target for the current phase
  of work. It is lower-stakes than production but still follows the same PR workflow.
- If you are ever unsure which branch you are on or which branch a task targets, run
  `git branch --show-current` and confirm with James before proceeding. Do not guess.

---

## 3. Absolute No-Touch Zones

The following are **working production interfaces**. They function correctly today and
serve live users. Do not modify, refactor, "clean up," or touch these unless James
explicitly names the file and describes the change:

- `templates/index.html` — single-tank calculator UI, **on the `Pricing_Engine_v1`
  production branch only.** See the explicit, scoped exception in Section 8.3 for
  UI/UX changes to this same file on the BETA branch.
- `templates/multi_tank.html` — multi-tank calculator UI (production). No BETA
  exception applies to this file — multi-tank is hidden in the BETA build entirely
  and is not being modified there.
- Any pricing logic, floor prices, unit weights, or quote-generation code paths in
  `app.py`, unless the task explicitly says otherwise
- Any route or function currently serving the internal estimating tool, when your task
  is scoped to the BETA/client-facing build

**Branch-scoped exception rule:** because `templates/index.html` is the same
file tracked across branches, an explicit authorization to edit it on the BETA branch
(Section 8.3) does NOT extend to the production branch. Work for that task stays on
the BETA branch. This branch must never be merged back into `Pricing_Engine_v1`
without James reviewing the UI/UX changes specifically for production-fitness first
— treat the BETA branch's version of this file as a deliberate fork, not a pending
production update.

**`app_multi.py` is dead code.** It is never executed by gunicorn. Do not edit it, do
not "fix" it, do not assume it is the source of truth for anything. All multi-tank
backend routes live in `app.py` exclusively.

If a task requires touching one of these files, treat that as a signal to pause and
confirm scope with James rather than proceeding.

---

## 4. Domain Constants (do not alter without explicit sign-off)

These values are load-bearing for every calculation in the tool. Treat any diff that
changes one of these as high-risk and flag it explicitly in your PR description.

- Module dimensions: 1.9685 ft × 3.937 ft (exactly 2:1 ratio)
- SC void ratio: 0.95486
- EX void ratio: 0.92633
- ASTM F2787 constants: m = 1.2, IM = 24.75%, LLDF = 1.15
- Factor of Safety minimums: 1.95 dead load / 1.75 live load
- PT-ROW flow coefficient: 0.464 CFS/crate
- Pallet counts: Base Unit 56/pallet, Side Plate 120/pallet, Bottom Plate 120/pallet,
  Pipe Connector 60/pallet, 12" Adapter 20/pallet, 16" Adapter 12/pallet
- Contingency units formula: `CEILING(base_units/56, 1) × 56 − base_units`

Reference standards governing modeling logic: ASTM F2787, OSHA 29 CFR 1926 Subpart P
Appendix B, Wavin AQ-100-08 Rev 2 (min/max cover), AQ-100-03.4 (liner protection),
AQ-100-25 (taco-wrap woven geometry), AQ-100-03.2 (tank envelope liner), AQ-200-01
Rev 4 (SC/EX per-layer height tables), AQ-100-24 (manhole setbacks).

---

## 5. Mandatory Pre-Commit / Pre-PR Audit

Run this full sequence before every commit that touches `.py` or template files. Do
not skip steps because a change "looks small." Include the results in your PR
description — pass/fail for each step, not just "tests passed."

1. `python3 -m py_compile <changed files>`
2. AST function-presence check — confirm every function you intended to keep/add is
   actually present and no function was accidentally orphaned or duplicated:
   `python3 -c "import ast; ..."` parse and list top-level defs
3. Orphaned-docstring sweep — check for docstrings left behind after a function body
   was removed or restructured
4. `grep -n` wiring audit — confirm every route, template variable, and form field
   referenced in templates actually has a corresponding backend variable, and vice
   versa. This codebase has a known failure mode: hidden form inputs placed outside
   `<form>` tags are silently dropped on POST. Any new hidden input must use
   `form="mainForm"` or be set via `formData.set()` in a fetch-based export.
5. Flask test client end-to-end run — actually exercise the modified route(s), not
   just import the module
6. Confirm no CSS variable references (e.g. `var(--highlight)`) were introduced into
   canvas-drawing code — these serialize as solid black on bare canvas. Use literal
   hex colors in any canvas context.
7. Confirm any new export/download logic sets the filename client-side via the
   `a.download` attribute — the browser overrides server `Content-Disposition`
   headers in this app's existing export flows, and inconsistency here has caused
   bugs before.
8. If the change involves numeric form inputs, confirm `0` is handled correctly.
   Flask's `or` operator coerces `0` to falsy — dedicated parsing helpers must be
   used wherever `0` is a valid input value, not a bare `request.form.get(...) or default`.

**Never deliver or commit a half-converted or partial edit.** If a mid-edit change
must be abandoned, restore the file to its last clean state and reapply cleanly —
do not leave a file in a mixed state, even temporarily, even on a feature branch.

---

## 6. Source-of-Truth Discipline

- Always clone/pull fresh from GitHub at the start of a session. Do not trust or
  reuse stale local file state from a previous session.
- Before editing, `grep` for key function names to confirm you are working in the
  correct base file — this codebase has had version/base-file confusion before
  (e.g. `app.py` vs. `app_multi.py`).
- `str_replace`-style edits require sufficiently unique multi-line anchors. Short or
  repeated patterns fail silently or match the wrong location — read surrounding
  context before editing, don't pattern-match on a single line.

---

## 7. Current BETA Build Context (client-facing, pricing-stripped)

This is the active project as of this handoff:

- **Goal:** invite-only client-facing BETA, live on Render, ~2 weeks before StormCon,
  running for a few weeks total before being taken down for feedback review.
- **Scope:** single-tank calculator only (multi-tank hidden), `PRICING_ENABLED=False`
  flag stripping all cost/quote fields down to a "Design Summary" PDF, and exactly
  **two** design tool calculators: Stage-Storage and the PT-ROW™ Transparent Sizing
  Calculator. Both are now confirmed and scoped — see Section 8 for build details on
  each. No third calculator is in scope for this BETA.
- **CAD/submittal deliverable:** NOT dynamic DWG/DXF generation. James has native DWG
  source files for all detail sheets (self-drawn). The build is a static file library:
  detail files live in a dedicated asset folder (not bloating git history with binary
  files via repeated commits — confirm storage approach with James, e.g. Render
  persistent disk or a dedicated non-git asset path), a config-to-filename lookup
  table maps tank configuration attributes to applicable detail sheets, and
  Python's `zipfile` bundles the selected files server-side via `send_file()`.
  There is no format-conversion problem here — do not build one.
- **Feedback mechanism:** a simple in-app "was this helpful / what broke" capture is
  planned — confirm exact implementation before building.
- **Kill switch:** a shutdown/expiry mechanism (env var or date check) should exist
  so the BETA can be taken offline without a manual scramble — confirm timing with
  James.
- Site Overlay (BETA feature) is explicitly OUT of scope for this client-facing
  build — it is beta-of-a-beta internally and considered too fragile for a first
  external impression.

---

## 8. Active Build Tasks for This Handoff

Two Design Tools calculators are confirmed in scope for the BETA (Section 7). Both
are documented below. Do not start either without confirming the exact BETA branch
name with James first (Section 2).

### 8.1 Stage-Storage Calculator — Integration Task

A **complete, working, standalone** Stage-Storage calculator has already been built
and delivered as a single self-contained HTML file (`AquaCell_Stage_Storage_Calculator.html`
— James will place this in the repo; confirm the path with him rather than assuming
one, e.g. it may land in `/static/` or a new `/design_tools/` asset folder). It is
fully functional on its own: no backend dependency, Chart.js loaded via CDN,
client-side CSV export via the `a.download` pattern (already consistent with the
rest of this codebase's export convention — do not change it).

**The task is integration, not rebuilding.** This tool needs to be added as a
tab/panel/card within `design_tools.html` alongside the existing design tools
dashboard, rather than remaining a separate standalone page. Specifics to confirm
with James before implementing, not to assume:
- Whether it's embedded as an iframe, or its markup/JS is merged directly into
  `design_tools.html`'s existing tab structure
- Whether the `window.addEventListener('DOMContentLoaded', ...)` auto-calculate-on-load
  behavior (which runs example values immediately) should still fire immediately, or
  only when the Stage-Storage tab is actually selected/activated — as built, it will
  run on page load regardless of which tab is visible, which may not be desired
  inside a multi-tab dashboard
- Whether this replaces any existing stage-storage logic already in
  `design_tools.html`, or this is the first stage-storage tool in that dashboard —
  check for a naming collision before assuming there's nothing to reconcile
- Confirm this calculator, once embedded, still needs to work identically inside
  the BETA (client-facing, pricing-stripped) build — it has no pricing content, so
  no `PRICING_ENABLED` gating should be needed, but confirm nothing else in the
  surrounding dashboard chrome leaks pricing context into the BETA version

Functionality already built and working in the delivered file (reference only — do
not redo this work, just carry it over intact):
- SC/EX configuration toggle with correct void ratios (0.95486 / 0.92633)
- Rectangular and Complex Shape (row-by-row, scaled-area) footprint modes
- Automatic snapping of entered footprint down to whole AquaCell module increments
- Full stage-storage table (elevation / tank storage / stone storage / total) with
  a configurable stage increment
- Base, cover, and perimeter stone storage, each independently includable/excludable
- Chart.js elevation-vs-storage curve
- CSV export and print-friendly layout

### 8.2 PT-ROW™ Transparent Sizing Calculator — New Build

Fully scoped and spec'd — see `PT-ROW_Transparent_Calculator_Spec.md` (James will
place this in the repo, e.g. `/docs/` or alongside other design tool specs; confirm
location). That spec is the authoritative build reference: it defines all four
sizing methods (Flow-Based, Volume-Based, Filter Area-Based, and a 4th rough-estimate
comparison method), the confirmed SC (10.25 ft³) and EX (10.83 ft³) net-storage
constants, fabric takeoff math for the Offset (Side-Car) layout only, and an
explicit out-of-scope list (Header Row / Inline / 3-Stack layouts — do not build
placeholder geometry for these, the source drawings don't exist yet).

Do not deviate from that spec's naming conventions — in particular, the Filter
Area-Based method must **not** be labeled "OEPA" anywhere in the client-facing UI,
per James's explicit direction, even though the underlying math is OEPA-derived.

### 8.3 Single-Tank Client-Facing UI/UX Update — PLANNED, THIRD SESSION

**Do not start this until Sessions 8.1 and 8.2 are complete, merged, and James has
confirmed he's satisfied with how those sessions went.** This is documented now so
the scope is captured, not because it's ready to build.

**Goal:** make the single-tank calculator the single, standalone client-facing tool
for the BETA. Multi-tank is hidden from the BETA build entirely — confirm with James
whether this task also formally removes any lingering multi-tank references/nav links
from the BETA build, or whether that's already handled elsewhere.

**Confirmed scope, at a high level (get full detail from James at session start,
do not assume beyond this):**
- Strip pricing from the single-tank UI itself — not just gating a `PRICING_ENABLED`
  flag server-side, but reviewing the actual template for pricing-adjacent UI
  elements, labels, or layout assumptions that should change now that cost fields
  won't render (e.g. layout regions sized around a pricing panel that will now be
  empty, or copy that references "quote" language that should shift to "design
  summary" language)
- General UI/UX layout pass for a first-time external engineer audience — this is
  explicitly a design/UX task, not just a pricing-removal task. Get specific
  direction from James on what "update" means before making layout decisions
  unilaterally — this is exactly the kind of ambiguous scope call that needs
  sign-off per Section 9, not a judgment call made independently.
- This work happens **only on the BETA branch**, per the branch-scoped exception in
  Section 3. Never touch `templates/index.html` on `Pricing_Engine_v1`.

**Before starting this session, confirm with James:**
- Exact target branch name
- Whether specific mockups/wireframes exist, or whether Claude Code is expected to
  propose layout options for review
- Whether this should reuse any visual language already established in
  `design_tools.html` or the Stage-Storage/PT-ROW tools from Sessions 8.1/8.2, for
  consistency across the BETA build

---

## 9. Communication Style

James works in all-caps for emphasis and values direct, efficient communication. He
wants complete, ready-to-deploy files — no snippets, no placeholders, no partial
diffs presented as finished work. He confirms all changes with live tests on Render
himself; when you cannot verify something end-to-end (e.g., no live Render
deployment access), say so explicitly rather than implying it was tested.

Surface ambiguous modeling, methodology, or scope decisions explicitly and get
sign-off before implementing — do not choose silently and present it as done.

---

## 10. Quick Reference — Stack

- Flask / Python backend
- HTML / JavaScript frontend (no frontend framework)
- ReportLab for PDF generation
- Chart.js for stage-storage curves
- PDF.js for PDF upload (Site Overlay — internal tool only, not in BETA scope)
- Canvas API for schematic/overlay rendering
- Render for deployment; `GITHUB_PAT` env var used for Details modal API calls and
  git push; `GITHUB_BRANCH` in `app.py` is the single line to change when cutting a
  new branch for a new deployment target
