# AquaCell (Internal) Crate Calculator — Development Roadmap

**Project Goal:**  
Professional web-based calculator for designing and quoting underground stormwater retention, detention, and infiltration systems using Wavin AquaCell polypropylene stackable crates. The tool must produce accurate storage volumes, BOM with part codes, geotextile quantities (burrito-style wrap), professional PDF reports, and visual layout drawings for client submittals.

**Current Status:** Live on Flask (production-ready core)

** ## Next Up (High Priority – Do These Next)**

##UPDATES FOR BOTH MULTI-TANK/SINGLE TANK CALCULATORS
- ADD DEFAULT NOTE IN SINGLE TANK: AQUACELL SYSTEM: SUBJECT TO CHANGE UPON APPROVAL OF PRELIMINARY DESIGN; SOURCE PLANS DATED
- COLLAPSABLE ADD TANK SIDEBAR ON MULTI TANK UI

##UPDATES FOR DESIGN TOOLS PAGE
- ADD DISTANCE FROM BUILDING CALCULATOR
- PT-ROW™ DESIGN CALCULATOR WITH VISUAL WITHIN A STRUCTURED TANK (OR BE ABLE TO PLACE BASED ON NUMBER OF CALCULATED CRATES)

**## Backlog / Future Enhancements (Add here when ideas come up)**
 - Metric toggle
 - Update font to Nunito (Wavin speciifc calculator ONLY)
 - UPDATE SINGLE TANK UI (EXPAND THE INPUTS AND REDUCE THE DASHBOARD DISPLAYS)

**Proposed Workflow & Calculator Evolution*
[ ] Pricing_Engine_v1 → git checkout -b Client_Facing_v1 → strip pricing
[ ] Pricing_Engine_v1 → git checkout -b MTC_Pricing → tweak MTC
[ ] Client_Facing_v1  → git checkout -b MTC_Client → strip MTC pricing

**Future Version Releases**
- (v16) UPLOAD PLAN SHEET AND CREATE AN OVERLAY OF TANK (RECTANGLE/SQUARE ONLY)
- (v18) Optional 3D isometric view of the crate layout
- (v19) SEPARATE WEB BASED CLIENT USER CALCULATOR TOOL WITH NO PRICING;
- (v20) ADD A DROP DOWN BOX TO CHANGE THE MANUFACTURER FROM THE CLIENT FACING TOOL TO RUN A COMPARISON OR EVENTUALLY BUILD A SINGLE CRATE COMPARISON CALCULATOR BETWEEN MFGR5
 -(V21) MOBILE FRIENDLY APP FOR QUICK ESTIMATING/ QUOTING. NO REPORTS GENERATED BUT OUTPUTS A JSON FILE TO ENTER INTO MAIN CALCULATOR TO CREATE REPORTS

- **##SYSTEM CHECKS TO VERIFY / CONFIRM**


- **DESIGN TOOLS PAGE** - COMPLETED**
-  [X] SEPARATE PAGE FOR "DESIGN TOOLS" MULTIPLE CALCULATORS LIVE AND EXPORT TO THEIR OWN PAGE WITH GRAPHICS
   - [X] bouyancy calculator
   - [X] MIN. COVER / BURIAL DEPTH
   - [X] LOADING MODEL (TRUCK ASTM F2787) & OUTRIGGER MODEL
   - [X] STEPPED PERIMETER CALCULATOR
   - [X] Excavation slope calculator integration (with variable H:V ratios) - lives on Dashboard used for estimating the amount of stone/fabric needed
   - [X] USE CANVAS TO BUILD A CONCEPTUAL TANK SYSTEM LAYOUT FOR COMPLEX CALCULATORS; CRATE BUILDER FOR COMPLEX (ENTER ROW BY ROW, THEN DESIGNATE HOW MANY CRATES IN THE ROW)

- **## PREVIOUS Version (v12-v15) — Completed**

-  [X] DRAG & DROP JSON FILE ENABLED FOR BOTH UI'S
-  [X] SINGLE TANK UI CHANGE GRID AND LINER SECTIONS TO "ENABLE" TOGGLE SIMILAR TO PT-ROW
-  [X] Conceptual schematic blocks designed to look like top view of AquaCell
-  [X] ADDED CLEAR DASHBOARD FUNCTION WITH REMINDER TO EXPORT FILE BEFORE STARTING NEW PROJECT
-  [X] BUG FIXED ON SINGLE-TANK LINER TOGGLE WHEN CALCULATE WAS EXECUTED IT WOULD TOGGLE OFF WHEN PREVIOUSLY FLIPPED ON
-  [X] UPDATE LARGE DIAMETER PIPE SELECTION - 30" PIPE IS MISSING FROM SELECTIONS
-  [X] WHENEVER PVC LINER IS ADDED, MAKE SURE THERE IS A CORRESPONDNG OUTER LAYER OF NON-WOVEN GEOTEXTILE ADDED TO THE OVERALL TOTALS OF FABRIC (SINGLE TANK/MULTI TANK)
-  [X] MULIT-TANK WHEN BACKFILL STONE INFILTRATION CHANGED TO ZERO, IT DOES NOT RESET ON EACH TANK, IT CARRIES OVER TO MULTIPLE TANKS. ALSO LINER SELECTIONS **CHCCK ALL***
-  [X] MULTI-TANK UPDATE ON SUMMARY UI CHANGE UNITS TO FT² FROM YD² (OR HAVE SEELCTION OPTION)
-  [X] EXPORT CSV PRICE SHEET WITH COMPONENT PRICING FOR CUSTOMER SERVICE
-  [X] ADD ADDITIONAL ACCESSORY FIELD FOR 18-36" GEO BOOTS. ENTER QUANTITY PASSES TO QUOTE SHEET
-  [X] FIXED MULTI TANK COPY TO EMAIL ACCESSORY WASTE LABELING (CHANGE TO DYNAMIC W/ EXCEPTION OF BACKFILL REMAINING AT 10%)
-  [X] command button to collapse all cards
-  [X] UPDATED PRICING CARD IN SINGLE TANK UI TO REMOVE NOTATIONS ON "FLOOR" AND "MARKUP" FROM CLIENT VIEW
-  [X] UPDATED CONTINGENCY CRATE CALCULATION TO ALLOW FOR DYNAMIC INPUT AND EDITING ON MULTI-TANK UI/UX
-  [X] ADD COMMAND BUTTON TO GITHUB LOCATION "DETAILS" SO PDF FOR THE PROJECT CAN BE COMPILED IN EACH INDIVIDUAL UI/UX
-  [X] SEPERATE QUOTE FROM TANK SUMMARY REPORT IN MUTLI-TANK UI
-  [X] ADD PROJECT NUMBER TO THE NAME EXTENSION FOR MULTI-QUOTE PDF
-  [X] REMOVE LINE ABOUT MARKET COST + 10% FROM MULTI-TANK PDF QUOTE SHEET
-  [X] ON MUTLI-TANK QUOTE SUMMARY, UPDATE PASS/FAIL FONTS TO RED/GREEN
-  [X] ADD STORAGE PASS/FAIL TO LEFT SIDE PANEL "TANKS IN PROJECT" INPUTS FOR EACH TANK CARD
-  [X] ADD PROJECT NOTES FOR EACH TANK TO ADD TO TANK SUMMARY PDF ONLY 
-  [X] ADD STORAGE PASS/FAIL TO LEFT SIDE PANEL "TANKS IN PROJECT" INPUTS FOR EACH TANK CARD
-  [X] ADD ALL ELEVATION LEVELS (TANK AND STONE) TO COVER & LOAD SECTION IN MULTI-TANK SUMMARY
-  [X] ADD BREAKDOWN OF STONE VOLUME IN EACH ELEVATION
-  [X] ALSO ADD THE FABRIC, GRID, STONE ACCESSORY BOM AMOUNTS TO (THIS TANK) PAGES APPENDED TO THE QUOTE 
-  [X] STAGE STORAGE DOWNLOAD FOR EACH TANK (DOES NOT HAVE TO BE ON THE RESULTS CARDS)
-  [X] ADD COMPLEX CALCULATOR (SIMPLE) TO MULTI TANK UI/UX
-  [X] ADD TOGGLE FOR VOID SPACE ENTRY
-  [X] ADD HARD CODED PRICING TO THE MULTI-TANK CALCULATOR
-  [X] ADDED FLOW RATE SIZING TO THE PT-ROW™ CARD
-  [X] ADD FABRIC AND ACCESSORY AMOUNTS TO THE MULTI-TANK QUOTE
-  [X] add place for Large Diameter Pipe Connections on quote sheet
-  [X] bottom of tank non-woven fabric quantity can be eliminated with Geogrid as a replacement (recommended)
-  [X] on MAIN DASHBOARD, RESULTS "STORAGE SUMMARY" CARD IS NOT CHANGING FONT COLOR FROM RED TO GREEN DURING PASS/FAIL AUDIT
-  [X] CONCEPTUAL SCHEMATIC NEEDS TO HAVE THE PROJECT NAME ADDED TO FILE EXTENSION
-  [X] add a COST PER CUBIC FOOT Field on the quote that is just a calculation of the total sum / AquaCell tank storage
-  [X] ADD WOVEN FABRIC FOR PT-ROW™ TANK
-  [X] add project name to details export name extension
-  [X] add biaxial geogrid total to the quote sheet as a product by others (based on excavation area + overage)
-  [X] add feature on UX/UI to toggle on/off layers of backfill included in storage
-  [X] stage storage table must be adjusted when the net stone backfill is adjusted
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
-  [X] fix contingency basin override
-  [X] v15 PRICING ENGINE THAT CAN BE EASILY ADJUSTED FOR DIST, MARKET, MSRP ESTIMATES. COMPONENT PRICING HARDCODED AND EDITABLE WHEN PRICE INCREASES OCCUR
-  [X] EXPORT STAGE STORAGE CURVE FROM UI
 


**How to use this file:**
- Open `ROADMAP.md` every time you work on the project.
- Move completed items to “Current Version” with [x].
- Add new ideas directly to “Backlog”.
- Update the Change Log with each meaningful update.

This single file replaces scattered notes, pencil sketches, and memory.
