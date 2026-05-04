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
  - 8-12" Pipe Connector (2476631200)
  - 12" Top Adapter (3085857)
- [x] Burrito-style geotextile logic (AquaCell tank + full stone envelope always wrapped, including bottom of base stone)
- [x] Geotextile section placed **below** Bill of Materials and **above** Price Estimation card
- [x] Geotextile Waste/Overlap (%) input with live multiplier
- [x] Improved PDF report:
  - Smaller, consistent fonts
  - Wavin logo restored at top
  - Full disclaimer visible at bottom
  - Clean one-page layout
- [x] JSON import/export for saving projects
- [x] Reset form and basic mobile compatibility
- [X] Multi-tank / multiple system support
- [X] Minimum Required Storage Volume input field added
- [X] Variable area input field that will allow for correct calculation of side plates when complex tank is scaled
- [X] Add project name to filename extension on all exported files (PDF, JSON, .CSV)
- [X] Amount of Top Adapters calculated based on total volume of tank calculated (=FLOOR(volume/3532,1)
- [X] Dark/light theme toggle for UI
- [X] Load / cover depth validation (H-10, HS-20, HS-25) with visual indicators (PASS/FAIL)
- [X] Qustion mark symbol which links to minimum requirements of the specific entry field (ex. perimeter, base stone)
- [X] Logo/banner on main UI page
- [X] Project Notes Field
- [X] PASS/FAIL VALIDATIONS (minimum/maximum depths (total install)
- [X] Add calculated safety factor (FoS) related to top cover depth for anything that is < the minimum cover recommended
- [X] **Visual Crate Generator**
- [X]  Matplotlib-generated Plan View (crate grid, snapped footprint label, perimeter stone highlight)
- [X]  When NO stage storage chart is selected, the PDF Summary generates one page, but disclaimer that appends the stage storage renders on the bottom of the 1st page over the geo wrap details.
- [X]  % waste on UI was zero, but calculated at 10% on PDF Summary report (FIX)
- [X]  Include Schematicc in PDF report (optional to user just like stage storage)
- [X]  When NO stage storage selected, only PAGE 1 SUMMARY PAGE generates. When Stage Storage IS selected, then pages 2 through whbatever is needed is appended to the Summary page. That way there is a clean separation between Summary Page | Stage Storage | and future Schematic Plan
- [X]  Part #'s for pipe connectors & adapters added back to UI and PDF Summary Report

 
 ## Next Up (High Priority – Do These Next)
 
-"?" pop-ups are missing from UI/UX main page
- update to v13 from V12 (GITHUB FILES ONLY)
- Scale bar + clean engineering style
- Section/Elevation View (stacked layers + compacted stone base)
- NOTES field needs to wrap text (currently now runs off page)
- NOTES field is not appearing on the Page 1 of PDF Summary



## Backlog / Future Enhancements (Add here when ideas come up)
- Stage storage table with CSV export
- Excavation slope calculator integration (with variable H:V ratios)
- System type selector (Retention / Detention / Infiltration) → auto-adjust geotextile, underdrain, liner options
- Optional 3D isometric view of the crate layout
- Integration of Wavin installation details (access ports, geogrid, compaction notes)
- Separate Quote Sheet & BOM Command buttons to generate individual pages with different disclaimers as needed
- hidden pages that prepends/appends when PDF is created (cover sheet, standard detail, specs, etc.)
- <div class="disclaimer">
        <strong>Disclaimer:</strong> This calculator provides <em>preliminary, conceptual estimates only</em> and is <em>not a stamped engineering design</em>. Wavin’s assistance in sizing or product selection is advisory and does not constitute design responsibility or guarantee system performance. The Engineer of Record (EoR) is solely responsible for verifying all design parameters and site conditions, including hydrology, structural requirements, soils, environmental factors, and integration with the overall stormwater system. AquaCell dimensions and assumptions (including usable storage and unit base areas) follow published product data. <strong>Final layouts, capacities, and installation depths must be confirmed by a licensed Professional Engineer</strong> using project‑specific plans (grading, pipe sizes and materials, invert elevations, loading conditions, and applicable codes/standards).
      </div>

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
