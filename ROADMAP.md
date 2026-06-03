# AquaCell v15 (Internal) Crate Calculator — Development Roadmap

**Project Goal:**  
Professional web-based calculator for designing and quoting underground stormwater retention, detention, and infiltration systems using Wavin AquaCell polypropylene stackable crates. The tool must produce accurate storage volumes, BOM with part codes, geotextile quantities (burrito-style wrap), professional PDF reports, and visual layout drawings for client submittals.

**Current Status:** Live on Flask (production-ready core)

**## PREVIOUS Version (v14) — Completed**

-  [X] reformat the UI/UX into "Cards"
-  [X] added Quick Summary copy/paste for body of emails
-  [X] add command to convert stone backfill & geotextile totals to desired dimensions (tons, cubic yards, cubic ft, square yards or ft)
-  [X] Add page up/down scroll button to get to the top or bottom of page faster (by sections or card groups)
-  [X] Add LIVE LOAD & DEAD LOAD calculations to Results UX (NOT ON PDF SUMMARY)
-  [X] Updated "scaled tank footprint area" in COMPLEX SHAPE to thousandths
-  [X] FIX COPY EMAIL FUNCTION TO ENSURE ALL FIELDS ARE BEING COPIED
-  [X] add a section in Stone Backfill card that shows stone calculated by LAYER (Top, Perimeter, Base) Gross/Net
-  [X] "This calculator provides <em>preliminary, conceptual estimates only</em> and is <em>not a stamped engineering design</em>. Wavin’s assistance in sizing or product selection is advisory and does not constitute             design responsibility or guarantee system performance. The Engineer of Record (EoR) is solely responsible for verifying all design parameters and site conditions, including hydrology, structural requirements,              soils, environmental factors, and integration with the overall stormwater system. AquaCell dimensions and assumptions (including usable storage and unit base areas) follow published product data. <strong>Final             layouts, capacities, and installation depths must be confirmed by a licensed Professional Engineer</strong> using project‑specific plans (grading, pipe sizes and materials, invert elevations, loading conditions,           and applicable codes/standards)." - ADDED TO BOTTOM OF CONCEPTUAL SCHEMATIC
-  [X] dedicated quote sheet and command export button
-  [X] Add freight calculator to take a percentage of the total cost (no results needed) just a built in calculator
-  [X] STREAMLINE PDF SUMMARY TO JUST ESSENTIALS (remove BOM and qquantities for estimation); summary only has the technical and could be included on the conceptual schematick page
-  [X] add excavated totals for area/ perimeter (for design layout tables)
-  [X] Fix DATE field on QUOTE PDF - "plans"
-  [X] Add "Top of Stone" to Cover & Load Verification Card under "Elevation Reference"
-  [X] Look at moving the Storage results card to top OF DASHBOARD TO MATCH REPORT
-  [X] REVISED EMAIL COPY/PASTE ACTION TO RELEVANT ESTIMATING VALUES
-  [X] Add Storage volume vs. Elevation graphic chart (x/y axis)- volume curve chart
-  [X] Stage storage table with CSV export with new command button at header
-  [X] add storage curve to bottom of stage storage on pdf export
-  [X] ADD EXTENSION ON THE NAMING CONVENTION OF THE REPORT AND QUOTE (DATE, VERSION, ETC.)
-  [X] Separate CLIENT & ESTIMATOR as idividual fields (customer/Wavin rep)
-  [X] add field for contingency units in BOM CARD and add line on quote sheet
-  [X] Add a pallet count estimator, weight estimator, and contingency calculator that can be added to the quote sheet or at least on dashboard when calculated
-  [X] Multiple Basin Entry and Reports branch created
-  [X] Add fabric and stone totals for both tanks to MULTI-TANK quote
-  [X] Add pricing to the CONTINGENCY BASINS as well. We should be encouraging those on every job.
-  [X] v15 PRICING ENGINE THAT CAN BE EASILY ADJUSTED FOR DIST, MARKET, MSRP ESTIMATES. COMPONENT PRICING HARDCODED AND EDITABLE WHEN PRICE INCREASES OCCUR
 
** ## Next Up (High Priority – Do These Next)**

- command button to collapse all cards
- optional geogrid totals (based on excavation area + overage)
- add place for "additional accessories" in BOM?

- System type selector (Retention / Detention / Infiltration) → auto-adjust geotextile, underdrain, liner options
- toggle DISTRIBUTOR, MARKET,  BUDGET ESTIMATE PRICING for quote page export (HEADER TITLE)

**##SYSTEM CHECKS TO VERIFY / CONFIRM**

**## Backlog / Future Enhancements (Add here when ideas come up)**

- Excavation slope calculator integration (with variable H:V ratios) - lives on Dashboard used for estimating the amount of stone/fabric needed (added to excavation & dimensional summary card
- bouyancy calculator
- List of details that can be selected that prepends/appends when PDF is created (cover sheet, standard detail, specs, etc.)
- QR codes or hyperlinks to wavin.us or specific installation details (probably on the cover or appended page)
- ADD USER INITIALS APPENDED TO A SELF-GENERATED OR USING THE EXISTING QUOTE # SYSTEM = PRODUCT + PROJECT# + "CREATED BY" INITIALS (EX. AQ1615-00-HD/1615-00-AQ_HD)
- Metric toggle
- Update font to Nunito (Wavin speciifc calculator ONLY)
- ADD A TOGGLE BOX UP IN THE PROJECT INFORMATION CARD FOR "REVISION" (REVISION WOULD BE ADDED TO THE QUOTE # "-01,-02, ETC.)

**Future Version Releases**

- (v16) UPLOAD PLAN SHEET AND CREATE AN OVERLAY OF TANK (RECTANGLE/SQUARE ONLY); OR CRATE BUILDER FOR COMPLEX (ENTER ROW BY ROW, THEN DESIGNATE HOW MANY CRATES IN THE ROW)
- (v17) USE CANVAS TO BUILD A CONCEPTUAL TANK SYSTEM LAYOUT FOR COMPLEX CALCULATORS
- (v18) Optional 3D isometric view of the crate layout 

**How to use this file:**
- Open `ROADMAP.md` every time you work on the project.
- Move completed items to “Current Version” with [x].
- Add new ideas directly to “Backlog”.
- Update the Change Log with each meaningful update.

This single file replaces scattered notes, pencil sketches, and memory.
