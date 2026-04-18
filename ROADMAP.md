# AquaCell V12 Crate Calculator — Development Roadmap

**Project Goal:**  
Professional web-based calculator for designing and quoting underground stormwater retention, detention, and infiltration systems using Wavin AquaCell polypropylene stackable crates. The tool must produce accurate storage volumes, BOM with part codes, geotextile quantities (burrito-style wrap), professional PDF reports, and visual layout drawings for client submittals.

**Current Status:** Live on Flask (production-ready core)

## Current Version (v12.1) — Completed
- [x] Core SC and EX configuration calculations (snapped tank dimensions, storage volumes)
- [x] Bill of Materials with correct part codes:
  - Base Unit (3091506)
  - Side Plate (2476600003)
  - Bottom Plate (2476600001)
- [x] Burrito-style geotextile logic (AquaCell tank + full stone envelope always wrapped, including bottom of base stone)
- [x] Geotextile section placed **below** Bill of Materials and **above** Price Estimation card
- [x] Geotextile Waste/Overlap (%) input with live multiplier
- [x] Improved PDF report:
  - Smaller, consistent fonts
  - Wavin logo restored at top
  - Full disclaimer visible at bottom
  - Clean one-page layout
- [x] Price estimation card (with hidden field for PDF)
- [x] JSON import/export for saving projects
- [x] Reset form and basic mobile compatibility

## Next Up (High Priority – Do These Next)
- [ ] **Visual Crate Generator**  
  - Matplotlib-generated Plan View (crate grid, snapped footprint label, perimeter stone highlight)  
  - Section/Elevation View (stacked layers + compacted stone base)  
  - Scale bar + clean engineering style  
  - “Download Layout PNG” button on results page  
  - Include in PDF report (optional)

## Backlog / Future Enhancements (Add here when ideas come up)
- Stage storage table (cumulative volume by elevation, with CSV export)
- Multi-tank / multiple system support
- Excavation slope calculator integration (with variable H:V ratios)
- Load / cover depth validation (H-10, HS-20, HS-25) with visual indicators
- System type selector (Retention / Detention / Infiltration) → auto-adjust geotextile, underdrain, liner options
- Auto quote number generation (e.g., AC-2026-001)
- Optional 3D isometric view of the crate layout
- Cost database with unit pricing persistence
- Print-friendly one-page layout improvements (12" increment tip already noted)
- Integration of Wavin installation details (access ports, geogrid, compaction notes)
- Dark/light theme toggle for UI

## Change Log
- **2026-04-18** – Improved PDF report (smaller fonts, restored logo, full disclaimer, better spacing). Geotextile section finalized below BOM.
- **2026-04-17** – Moved geotextile below BOM, added part codes to Bill of Materials, implemented burrito-style geotextile wrap.
- **2026-04-16** – Initial V12 Flask web app with SC/EX logic, storage calculations, BOM, price card, and basic PDF.

## Notes & Design Decisions
- Geotextile is always “burrito style” (tank fully wrapped + stone envelope fully wrapped, including bottom of base stone)
- Geotextile section stays below Bill of Materials for better visual flow
- PDF must remain clean and one-page when possible
- All calculations must match legacy Excel as closely as possible
- Visual generator should match the clean engineering style of the sample PNG (light blue crates, red perimeter line, layered section)

---

**How to use this file:**
- Open `ROADMAP.md` every time you work on the project.
- Move completed items to “Current Version” with [x].
- Add new ideas directly to “Backlog”.
- Update the Change Log with each meaningful update.

This single file replaces scattered notes, pencil sketches, and memory.
