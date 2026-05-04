from flask import Flask, render_template, request, send_file
import io
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import datetime
import math
import os
import base64
from io import BytesIO

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def index():
    results = None
    form_data = {}

    if request.method == 'POST':
        project_name = request.form.get('project_name', '')
        project_num = request.form.get('project_num', '')
        location = request.form.get('location', '')
        client = request.form.get('client', '')
        config = request.form.get('config', 'SC')
        layers = int(request.form.get('layers', 3))
        surface_elev = float(request.form.get('surface_elev', 0) or 0)
        tank_bottom_elev = float(request.form.get('tank_bottom_elev', 0) or 0)
        traffic_load = request.form.get('traffic_load', 'HS20')
        known_width = float(request.form.get('known_width', 0) or 0)
        known_length = float(request.form.get('known_length', 0) or 0)
        tank_perimeter = request.form.get('tank_perimeter', '').strip()
        perimeter_stone_width = float(request.form.get('perimeter_stone_width', 1.0) or 1.0)
        cover_stone = float(request.form.get('cover_stone', 1.0) or 1.0)
        base_stone = float(request.form.get('base_stone', 0.333) or 0.333)
        min_storage = float(request.form.get('min_storage', 0) or 0)
        stone_void = float(request.form.get('stone_void', 0.40) or 0.40)
        geoWaste = int(request.form.get('geoWaste', 10) or 10)
        pipe_connectors = int(request.form.get('pipe_connectors', 0) or 0)
        top_adapters_12 = int(request.form.get('top_adapters_12', 0) or 0)
        project_notes = request.form.get('project_notes', '')
        include_stage_storage = request.form.get('include_stage_storage') == 'yes'
        include_schematic = request.form.get('include_schematic') == 'yes'
        stage_increment_in = int(request.form.get('stage_increment_in', 12) or 12)

        # Core calculations (unchanged)
        MODULE_WID = 1.9685
        MODULE_LEN = 3.937
        crates_wide = math.floor(known_width / MODULE_WID)
        crates_long = math.floor(known_length / MODULE_LEN)
        tank_width = crates_wide * MODULE_WID
        tank_length = crates_long * MODULE_LEN

        if config == 'SC':
            layer_heights = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
            void_ratio = 0.95486
            side_multiplier = 1.312336
        else:
            layer_heights = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
            void_ratio = 0.92633
            side_multiplier = 1.509186351

        tank_height = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]

        num_crates = crates_wide * crates_long * layers
        gross_tank_vol = num_crates * (MODULE_WID * MODULE_LEN * tank_height / layers)
        tank_storage = gross_tank_vol * void_ratio

        outer_width = tank_width + 2 * perimeter_stone_width
        outer_length = tank_length + 2 * perimeter_stone_width
        total_system_depth = base_stone + tank_height + cover_stone
        total_excavation_vol = outer_width * outer_length * total_system_depth
        total_stone_storage = (total_excavation_vol - gross_tank_vol) * stone_void
        total_storage = tank_storage + total_stone_storage

        used_perimeter = 2 * (tank_width + tank_length)
        tank_perim_calc = float(tank_perimeter) if tank_perimeter else used_perimeter
        side_plates = round(tank_perim_calc * (layers * side_multiplier) / 5.17)

        if config == 'SC':
            base_units = num_crates
            bottom_plates = crates_wide * crates_long
        else:
            base_units = num_crates * 2
            bottom_plates = 0

        tank_top_bottom_area = 2 * tank_width * tank_length
        tank_sides_area = used_perimeter * tank_height
        geoTank = round((tank_top_bottom_area + tank_sides_area) * (1 + geoWaste / 100.0), 1)
        geoStone = round((outer_width * outer_length * 2 + outer_width * total_system_depth * 2 + outer_length * total_system_depth * 2) * (1 + geoWaste / 100.0), 1)
        geoTotal = round(geoTank + geoStone, 1)

        stone_backfill_bulk_ft3 = round(total_stone_storage * 1.10, 1)
        stone_backfill_bulk_yd3 = round(stone_backfill_bulk_ft3 / 27, 2)

        stage_storage = None
        if include_stage_storage:
            stage_storage = []
            increment_ft = stage_increment_in / 12.0
            top_of_stone = tank_bottom_elev + tank_height + cover_stone
            current_elev = tank_bottom_elev - base_stone
            while current_elev <= top_of_stone + 0.01:
                depth_in_tank = max(0, min(tank_height, current_elev - tank_bottom_elev))
                tank_vol_at_elev = (depth_in_tank / tank_height) * tank_storage if tank_height > 0 else 0
                depth_in_stone = max(0, min(total_system_depth, current_elev - (tank_bottom_elev - base_stone)))
                stone_vol_at_elev = (depth_in_stone / total_system_depth) * total_stone_storage if total_system_depth > 0 else 0
                total_vol_at_elev = tank_vol_at_elev + stone_vol_at_elev

                stage_storage.append({
                    'elevation_ft': round(current_elev, 2),
                    'tank_storage': round(tank_vol_at_elev, 1),
                    'stone_storage': round(stone_vol_at_elev, 1),
                    'total_storage': round(total_vol_at_elev, 1)
                })
                current_elev += increment_ft

        cover_depth = round(surface_elev - (tank_bottom_elev + tank_height), 2)
        max_cover_req = 20 if config == 'SC' else 30
        cover_status = 'PASS' if cover_depth <= max_cover_req else 'FAIL'
        dead_load_psi = round(cover_stone * 120 / 144, 2)
        max_compressive = 70 if config == 'SC' else 100
        fos_dead = round(max_compressive / dead_load_psi, 2) if dead_load_psi > 0 else None

        results = {
            'config': config,
            'layers': layers,
            'surface_elev': surface_elev,
            'tank_bottom_elev': tank_bottom_elev,
            'tank_top_elev': round(tank_bottom_elev + tank_height, 2),
            'cover_depth': cover_depth,
            'cover_status': cover_status,
            'max_cover_req': max_cover_req,
            'max_cover_status': cover_status,
            'dead_load_psi': dead_load_psi,
            'fos_dead': fos_dead,
            'traffic_load': traffic_load,
            'tank_width': round(tank_width, 2),
            'tank_length': round(tank_length, 2),
            'tank_height': round(tank_height, 2),
            'tank_storage': round(tank_storage, 1),
            'stone_storage': round(total_stone_storage, 1),
            'total_storage': round(total_storage, 1),
            'used_perimeter': round(used_perimeter, 2),
            'base_units': base_units,
            'side_plates': side_plates,
            'bottom_plates': bottom_plates,
            'pipe_connectors': pipe_connectors,
            'top_adapters_12': top_adapters_12,
            'project_notes': project_notes,
            'stage_storage': stage_storage,
            'stage_increment_in': stage_increment_in,
            'min_storage': min_storage if min_storage > 0 else None,
            'storage_status': 'PASS' if min_storage <= total_storage else 'FAIL' if min_storage > 0 else None,
            'stone_backfill_bulk_ft3': stone_backfill_bulk_ft3,
            'stone_backfill_bulk_yd3': stone_backfill_bulk_yd3,
            'geoTank': geoTank,
            'geoStone': geoStone,
            'geoTotal': geoTotal,
            'geoWaste': geoWaste,
            'include_schematic': include_schematic,
        }

        form_data = request.form

    return render_template('index.html', results=results, form_data=form_data)

@app.route('/download_pdf', methods=['POST'])
def download_pdf():
    project_name = request.form.get('project_name', 'Project')
    config = request.form.get('config', 'SC')
    layers = int(request.form.get('layers', 3))
    surface_elev = float(request.form.get('surface_elev', 0) or 0)
    tank_bottom_elev = float(request.form.get('tank_bottom_elev', 0) or 0)
    traffic_load = request.form.get('traffic_load', 'HS20')
    known_width = float(request.form.get('known_width', 0))
    known_length = float(request.form.get('known_length', 0))
    perimeter_stone_width = float(request.form.get('perimeter_stone_width', 1.0))
    cover_stone = float(request.form.get('cover_stone', 1.0))
    base_stone = float(request.form.get('base_stone', 0.333))
    stone_void = float(request.form.get('stone_void', 0.40))
    geoWaste = int(request.form.get('geoWaste', 10) or 10)
    pipe_connectors = int(request.form.get('pipe_connectors', 0))
    top_adapters_12 = int(request.form.get('top_adapters_12', 0))
    total_aquacell_cost = request.form.get('totalAquaCellCost', '—')
    include_stage_storage = request.form.get('include_stage_storage') == 'yes'
    include_schematic = request.form.get('include_schematic') == 'yes'
    schematic_image = request.form.get('schematic_image')
    stage_increment_in = int(request.form.get('stage_increment_in', 12) or 12)

    # Same calculations as main route
    MODULE_WID = 1.9685
    MODULE_LEN = 3.937
    crates_wide = math.floor(known_width / MODULE_WID)
    crates_long = math.floor(known_length / MODULE_LEN)
    tank_width = crates_wide * MODULE_WID
    tank_length = crates_long * MODULE_LEN

    if config == 'SC':
        layer_heights = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
        void_ratio = 0.95486
        side_multiplier = 1.312336
    else:
        layer_heights = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
        void_ratio = 0.92633
        side_multiplier = 1.509186351

    tank_height = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]

    num_crates = crates_wide * crates_long * layers
    gross_tank_vol = num_crates * (MODULE_WID * MODULE_LEN * tank_height / layers)
    tank_storage = gross_tank_vol * void_ratio

    outer_width = tank_width + 2 * perimeter_stone_width
    outer_length = tank_length + 2 * perimeter_stone_width
    total_system_depth = base_stone + tank_height + cover_stone
    total_excavation_vol = outer_width * outer_length * total_system_depth
    total_stone_storage = (total_excavation_vol - gross_tank_vol) * stone_void
    total_storage = tank_storage + total_stone_storage

    used_perimeter = 2 * (tank_width + tank_length)
    tank_perimeter = used_perimeter
    side_plates = round(tank_perimeter * (layers * side_multiplier) / 5.17)

    if config == 'SC':
        base_units = num_crates
        bottom_plates = crates_wide * crates_long
    else:
        base_units = num_crates * 2
        bottom_plates = 0

    tank_top_bottom_area = 2 * tank_width * tank_length
    tank_sides_area = used_perimeter * tank_height
    geoTank = round((tank_top_bottom_area + tank_sides_area) * (1 + geoWaste / 100.0), 1)
    geoStone = round((outer_width * outer_length * 2 + outer_width * total_system_depth * 2 + outer_length * total_system_depth * 2) * (1 + geoWaste / 100.0), 1)
    geoTotal = round(geoTank + geoStone, 1)

    stone_backfill_bulk_ft3 = round(total_stone_storage * 1.10, 1)
    stone_backfill_bulk_yd3 = round(stone_backfill_bulk_ft3 / 27, 2)

    stage_storage_lines = []
    if include_stage_storage:
        increment_ft = stage_increment_in / 12.0
        top_of_stone = tank_bottom_elev + tank_height + cover_stone
        current_elev = tank_bottom_elev - base_stone
        while current_elev <= top_of_stone + 0.01:
            depth_in_tank = max(0, min(tank_height, current_elev - tank_bottom_elev))
            tank_vol = (depth_in_tank / tank_height) * tank_storage if tank_height > 0 else 0
            depth_in_stone = max(0, min(total_system_depth, current_elev - (tank_bottom_elev - base_stone)))
            stone_vol = (depth_in_stone / total_system_depth) * total_stone_storage if total_system_depth > 0 else 0
            total_vol = tank_vol + stone_vol
            stage_storage_lines.append((round(current_elev, 2), round(tank_vol, 1), round(stone_vol, 1), round(total_vol, 1)))
            current_elev += increment_ft

    # Build PDF
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 70

    # Logo + Timestamp + Header + Disclaimer + Summary (Page 1)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if os.path.exists(logo_path):
        img = ImageReader(logo_path)
        c.drawImage(img, 50, y - 30, width=180, height=60, preserveAspectRatio=True, mask='auto')

    c.setFont("Helvetica", 9)
    timestamp = f"Generated {datetime.datetime.now().strftime('%m/%d/%Y %H:%M')}"
    c.drawRightString(width - 50, y - 10, timestamp)

    y -= 90

    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, y, "AquaCell V12 Crate Calculator")
    y -= 22
    c.setFont("Helvetica", 11)
    c.drawString(50, y, "Underground Stormwater Retention / Detention / Infiltration System")
    y -= 18

    c.setFont("Helvetica", 8)
    c.drawString(50, y, "Disclaimer: This calculator provides preliminary, conceptual estimates only and is not a stamped engineering")
    y -= 12
    c.drawString(50, y, "design. The Engineer of Record is solely responsible for final design and verification.")
    y -= 22

    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"Project Name: {project_name}   Configuration: {config} Configuration ({layers} Layers)")
    y -= 18
    c.drawString(50, y, f"Surface Elevation: {surface_elev} ft")
    y -= 18
    c.drawString(50, y, f"Tank Bottom Elevation: {tank_bottom_elev} ft")
    y -= 18
    c.drawString(50, y, f"Tank Top Elevation: {round(tank_bottom_elev + tank_height, 2)} ft")
    y -= 18

    cover_depth = round(surface_elev - (tank_bottom_elev + tank_height), 2)
    max_cover_req = 20 if config == 'SC' else 30
    cover_status = 'PASS' if cover_depth <= max_cover_req else 'FAIL'
    dead_load_psi = round(cover_stone * 120 / 144, 2)
    max_compressive = 70 if config == 'SC' else 100
    fos_dead = round(max_compressive / dead_load_psi, 2) if dead_load_psi > 0 else None

    c.drawString(50, y, f"Actual Cover Depth: {cover_depth} ft → {cover_status}")
    y -= 18
    c.drawString(50, y, f"Maximum Allowable Cover Depth: {max_cover_req} ft → {cover_status}")
    y -= 18
    c.drawString(50, y, f"Dead Load Pressure: {dead_load_psi} psi")
    y -= 18
    c.drawString(50, y, f"Factor of Safety (Dead Load): {fos_dead if fos_dead else '—'}")
    y -= 18
    c.drawString(50, y, f"Traffic Load: {traffic_load}")
    y -= 25

    lines = [
        f"Snapped Tank: {round(tank_width,2)} ft × {round(tank_length,2)} ft × {round(tank_height,2)} ft",
        f"AquaCell Tank Storage: {round(tank_storage,1)} ft³",
        f"Stone Storage: {round(total_stone_storage,1)} ft³",
        f"Total System Storage: {round(total_storage,1)} ft³",
        "",
        f"Estimated Stone Backfill Volume to Purchase: {stone_backfill_bulk_ft3} ft³ ({stone_backfill_bulk_yd3} yd³)",
        "",
        "Bill of Materials",
        f"Base Unit (3091506) ................ {base_units}",
        f"Side Plate (2476600003) ............ {side_plates}",
        f"Bottom Plate (2476600001) .......... {bottom_plates}",
        f"8-12\" Pipe Connectors (2476631200) ............... {pipe_connectors}",
        f"12\" Top Adapters (3085857) .................... {top_adapters_12}",
        "",
        "Geotextile Fabric (Burrito Wrap)",
        f"AquaCell Only .................... {geoTank} ft²",
        f"Stone Envelope .................... {geoStone} ft²",
        f"Total Geotextile .................... {geoTotal} ft²",
        f"Waste/Overlap .................... {geoWaste}.0%",
    ]

    for line in lines:
        c.drawString(50, y, line)
        y -= 18

    c.setFont("Helvetica", 9)
    c.drawRightString(width - 50, 70, "Page 1 of 2" if include_stage_storage else "Page 1 of 1")

    # Stage Storage (if enabled)
    if include_stage_storage and stage_storage_lines:
        c.showPage()
        y = height - 70
        c.setFont("Helvetica-Bold", 14)
        c.drawString(50, y, f"Stage Storage Table ({stage_increment_in}.0 inch increments)")
        y -= 30

        c.setFont("Helvetica-Bold", 10)
        c.drawString(50, y, "Elevation (ft)")
        c.drawRightString(240, y, "Tank Storage (ft³)")
        c.drawRightString(355, y, "Stone Storage (ft³)")
        c.drawRightString(480, y, "Total Storage (ft³)")
        y -= 20

        c.setFont("Helvetica", 9)
        page_num = 2
        for elev, tank, stone, total in stage_storage_lines:
            if y < 100:
                c.showPage()
                y = height - 70
                page_num += 1
                c.setFont("Helvetica-Bold", 10)
                c.drawString(50, y, f"Stage Storage Table ({stage_increment_in}.0 inch increments) (continued)")
                y -= 30
                c.setFont("Helvetica-Bold", 10)
                c.drawString(50, y, "Elevation (ft)")
                c.drawRightString(240, y, "Tank Storage (ft³)")
                c.drawRightString(355, y, "Stone Storage (ft³)")
                c.drawRightString(480, y, "Total Storage (ft³)")
                y -= 20
                c.setFont("Helvetica", 9)
            c.drawString(50, y, f"{elev}")
            c.drawRightString(240, y, f"{tank}")
            c.drawRightString(355, y, f"{stone}")
            c.drawRightString(480, y, f"{total}")
            y -= 15

        c.setFont("Helvetica", 9)
        c.drawRightString(width - 50, 70, f"Page {page_num} of {page_num}")

    # Schematic Layout as LAST page (if enabled)
    if include_schematic and schematic_image:
        c.showPage()
        y = height - 50
        c.setFont("Helvetica-Bold", 14)
        c.drawString(50, y, "Conceptual Plan View / Section Schematic")
        y -= 40

        try:
            image_data = base64.b64decode(schematic_image.split(',')[1])
            img = ImageReader(BytesIO(image_data))
            c.drawImage(img, 50, y - 650, width=width-100, height=650, preserveAspectRatio=True, mask='auto')
        except Exception:
            c.setFont("Helvetica", 10)
            c.drawString(50, y - 30, "Schematic image could not be rendered")

        c.setFont("Helvetica", 9)
        c.drawString(50, 70, f"Generated {datetime.datetime.now().strftime('%m/%d/%Y %H:%M')}")
        c.drawRightString(width - 50, 70, f"Page {page_num + 1 if include_stage_storage else 2} of {page_num + 1 if include_stage_storage else 2}")

    c.save()

    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="AquaCell_V12_Report.pdf", mimetype='application/pdf')

if __name__ == '__main__':
    app.run(debug=True)
