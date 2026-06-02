# AquaCell v14 Crate Calculator — Development Roadmap

**Project Goal:**  
Professional web-based calculator for designing and quoting underground stormwater retention, detention, and infiltration systems using Wavin AquaCell polypropylene stackable crates. The tool must produce accurate storage volumes, BOM with part codes, geotextile quantities (burrito-style wrap), professional PDF reports, and visual layout drawings for client submittals.

**Current Status:** Live on Flask (production-ready core)

**## Previous Version (v12.1) — Completed**
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
- [X]  When NO stage storage selected, only PAGE 1 SUMMARY PAGE generates. When Stage Storage IS selected, then pages 2 through whbatever is needed is appended to the Summary page. That way there is a clean separation            between Summary Page | Stage Storage | and future Schematic Plan
- [X]  Part #'s for pipe connectors & adapters added back to UI and PDF Summary Report
- [X]  "?" pop-ups are missing from UI/UX main page
- [X]  NOTES field needs to wrap text (currently now runs off page); ALSO MOVED TO BOTTOM OF PAGE
- [X]  PDF SUMMARY PAGE CHANGES
    -  [X] Removed Dead Load calculated totals
    -  [X] Moved Stone Backfill to below BOM
    -  [X] Changed to note 10% added to bacfill volume total
    -  [X] Removed "Burrito Wrap" from the Geotextile Fabric header
    -  [X] Added square yards to the geotextile totals.
    -  [X] Add minimum storage volume to PDF Takeoff Report (on results card)
-  [X] UPDATE COVER DEPTH MAX NOTE FOR PASS/FAIL SC & EX (14.4,26.2)
-  [X] COVER STONE DEPTH NEEDS TO ROUND TO HUNDRETHS, NOT TENTHS (??)
-  [X] FIX SECTION IN DARK MODE THAT IS WASHED OUT (SCHEMATIC & STAGE STORAGE SELECTION CARD)
-  [X] Scale bar + clean engineering style
-  [X] Section/Elevation View (stacked layers + compacted stone base)
-  [X] Add page up/down scroll button to get to the top or bottom of page faster (by sections or card groups)
-  [X] update to v14 from V12
-  [X] side panel that stays staic with executable command buttons (Calculate, Reset, download, export, etc.)


**## CURRENT Version (v14) — Completed**

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
 
** ## Next Up (High Priority – Do These Next)**

- command button to collapse all cards
- optional geogrid totals (based on excavation area + overage)
- add place for "additional accessories" in BOM?
- Excavation slope calculator integration (with variable H:V ratios) - lives on Dashboard used for estimating the amount of stone/fabric needed (added to excavation & dimensional summary card
- bouyancy calculator
- System type selector (Retention / Detention / Infiltration) → auto-adjust geotextile, underdrain, liner options
- toggle DISTRIBUTOR, MARKET,  BUDGET ESTIMATE PRICING for quote page export (HEADER TITLE)

**##SYSTEM CHECKS TO VERIFY / CONFIRM**
- ask Claude about arriving at live load FOS calculations to be sure it aligns correctly with excel load calculator


**## Backlog / Future Enhancements (Add here when ideas come up)**

- (v15) PRICING ENGINE THAT CAN BE EASILY ADJUSTED FOR DIST, MARKET, MSRP ESTIMATES. ALLOW FOR UPLOADING OF COMPONENT PRICING OR MULTIPLIER WHEN PRICE INCREASES OCCUR
- (v16) UPLOAD PLAN SHEET AND CREATE AN OVERLAY OF TANK (RECTANGLE/SQUARE ONLY); OR CRATE BUILDER FOR COMPLEX (ENTER ROW BY ROW, THEN DESIGNATE HOW MANY CRATES IN THE ROW)
- (v17) USE CANVAS TO BUILD A CONCEPTUAL TANK SYSTEM LAYOUT FOR COMPLEX CALCULATORS
- (v18) Optional 3D isometric view of the crate layout 
- Integration of Wavin installation details (access ports, geogrid, compaction notes)
- List of details that can be selected that prepends/appends when PDF is created (cover sheet, standard detail, specs, etc.)
- QR codes or hyperlinks to wavin.us or specific installation details (probably on the cover or appended page)
- ADD USER INITIALS APPENDED TO A SELF-GENERATED OR USING THE EXISTING QUOTE # SYSTEM = PRODUCT + PROJECT# + "CREATED BY" INITIALS (EX. AQ1615-00-HD/1615-00-AQ_HD)
- Metric toggle
- Update font to Nunito (Wavin speciifc calculator ONLY)
- ADD A TOGGLE BOX UP IN THE PROJECT INFORMATION CARD FOR "REVISION" (REVISION WOULD BE ADDED TO THE QUOTE # "-01,-02, ETC.)

**How to use this file:**
- Open `ROADMAP.md` every time you work on the project.
- Move completed items to “Current Version” with [x].
- Add new ideas directly to “Backlog”.
- Update the Change Log with each meaningful update.

This single file replaces scattered notes, pencil sketches, and memory.
