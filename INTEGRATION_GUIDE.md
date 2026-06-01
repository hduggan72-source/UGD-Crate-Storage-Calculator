# Multi-Tank Calculator — Integration Guide
## Branch: `Multi_Tank_Calculator`

---

## Files Delivered

| File | Destination | Action |
|------|-------------|--------|
| `app_multi.py` | Merge into `app.py` | Copy the 3 routes + helpers |
| `multi_tank.html` | `templates/` folder | Copy alongside `index.html` |

---

## Step 1 — Create the Branch

```bash
git checkout Quote_Report_Summary_Exports
git checkout -b Multi_Tank_Calculator
```

---

## Step 2 — Copy `multi_tank.html`

Place `multi_tank.html` in your `templates/` folder alongside `index.html`. No Jinja variables needed — the template is fully self-contained (all data flows via JS ↔ Flask JSON API).

---

## Step 3 — Merge `app_multi.py` into `app.py`

### 3a. Add these imports at the top of `app.py` (if not already present)

```python
import json
```
All other imports (`math`, `io`, `datetime`, `os`, `colors`, etc.) are already in your app.py.

### 3b. Add the constants block

After the existing color/layout constants in `app.py`, paste:

```python
# ── Multi-Tank shared constants ──
CONFIG_DATA = {
    'SC': {
        'layer_heights':   [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581],
        'void_ratio':      0.95486,
        'side_multiplier': 1.312336,
        'max_strength':    70,
        'min_cover':       {'H10': 1.0, 'HS20': 1.5,  'HS25': 2.5},
        'max_cover':       14.4,
    },
    'EX': {
        'layer_heights':   [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073],
        'void_ratio':      0.92633,
        'side_multiplier': 1.509186351,
        'max_strength':    100,
        'min_cover':       {'H10': 1.0, 'HS20': 1.33, 'HS25': 1.83},
        'max_cover':       26.2,
    },
}
_MT_PALLETS = { 'base':56, 'side':120, 'bottom':120, 'pipe':60, 'adapter12':20, 'adapter16':12 }
_MT_WEIGHTS = { 'base':25.18, 'side':4.91, 'bottom':7.86, 'pipe':2.70, 'adapter12':11.02, 'adapter16':22.00 }
```

### 3c. Paste the three functions and three routes

From `app_multi.py`, copy everything below the line:
```
# ══════ CALC ENGINE ══════
```
...through the end of the file (the `multi_index`, `multi_calculate`, and `multi_download_quote` routes, plus `calc_tank` and `cumulative_bom`).

**Remove** the `app = Flask(__name__)` and `if __name__ == '__main__':` lines — those already exist in your `app.py`.

### 3d. Update `GITHUB_BRANCH` constant

```python
GITHUB_BRANCH = "Multi_Tank_Calculator"
```

---

## Step 4 — Add Nav Link to `index.html`

In the topbar actions section of `index.html`, add a link to the multi-tank tool:

```html
<a href="/multi" class="topbar-btn" style="background:#0891b2;color:white;text-decoration:none;">
    🏗️ Multi-Tank
</a>
```

And in `multi_tank.html`, the `← Single Tank` link already points to `/`.

---

## Step 5 — Test Locally

```bash
python app.py
# Navigate to http://localhost:5000/multi
```

Verify:
- [ ] `+ Add Tank` button creates a tank in the sidebar
- [ ] Tank editor loads with correct defaults
- [ ] Calculate button calls `/multi/calculate` and renders results
- [ ] Cumulative BOM panel updates after each calculation
- [ ] Download Quote generates a PDF with per-tank detail pages
- [ ] Export / Import JSON round-trips correctly

---

## Architecture Summary

```
/multi              GET   → renders multi_tank.html (static, no Jinja vars)
/multi/calculate    POST  → JSON in: [{tank dict}, ...] → JSON out: {tank_results, cumulative, bom_detail}
/multi/download_quote POST → JSON in: {project + tanks} → PDF out: multi-page quote
```

### Data Flow

```
JS (multi_tank.html)
  │
  ├─ tanks[]    — array of input dicts (in-memory state)
  ├─ results[]  — array of calc result dicts (null until calculated)
  │
  ├─ POST /multi/calculate → server runs calc_tank() for each tank
  │                        → returns tank_results + cumulative BOM
  │
  └─ POST /multi/download_quote → server recalculates from raw tank inputs
                                 → generates ReportLab PDF
                                 → streams back as download
```

### JSON Export Schema (v1)

```json
{
  "version": 1,
  "exported": "2025-01-15T14:30:00.000Z",
  "project": {
    "name": "Riverside Commons",
    "num": "2025-042",
    "client": "City of Anytown",
    "location": "Atlanta, GA",
    "estimator": "Jane Smith",
    "estimator_email": "jane@wavin.com",
    "notes": "Phase 1 detention"
  },
  "pricing": {
    "cost_per_ft3": 5.19,
    "freight_pct": 10
  },
  "tanks": [
    { "tank_label": "Basin A", "config": "SC", "layers": "5", ... },
    { "tank_label": "Basin B", "config": "EX", "layers": "3", ... }
  ],
  "results": [ { ...calc results... }, { ...calc results... } ]
}
```

The JSON export feeds directly into the existing single-tank quote system by iterating `tanks[]` if you want to generate individual quotes per tank from the multi-tank session.

---

## What Is NOT Changed

- `app.py` existing routes (`/`, `/download_pdf`, `/download_quote`, `/download_stage_csv`, `/api/details`, `/download_details_pdf`) are untouched
- `index.html` is untouched (except optionally adding the nav link)
- Render deployment: the existing single-tank tool stays live on `Quote_Report_Summary_Exports` while you build/test `Multi_Tank_Calculator`

---

## Known Limitations / Next Steps

1. **Complex Shape mode** — Multi-tank currently supports rectangle tanks only (the most common case). Complex shape can be added per-tank in a future iteration.
2. **Stage Storage** — Not included in multi-tank (each tank would need its own table). Can be added as an optional per-tank export.
3. **Schematic** — The per-tank schematic canvas is not included; the PDF uses a text-based detail page per tank instead.
4. **Pricing** — The cumulative quote prices the whole order at one $/ft³ rate. Tank-by-tank pricing can be added if needed.
