from flask import Flask, render_template, request, send_file
import io
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import datetime
import math
import os

app = Flask(__name__)

# Minimum cover requirements (ft) from Wavin docs
MIN_COVER = {
    'SC': {'H10': 1.0, 'HS20': 1.5, 'HS25': 2.5},
    'EX': {'H10': 1.0, 'HS20': 1.33, 'HS25': 1.83}
}

@app.route('/', methods=['GET', 'POST'])
def index():
    results = None
    form_data = {}

    if request.method == 'POST':
        # Read form data safely
        project_name = request.form.get('project_name', '')
        project_num = request.form.get('project_num', '')
        location = request.form.get('location', '')
        client = request.form.get('client', '')
        config = request.form.get('config', 'SC')
        layers = int(request.form.get('layers') or 3)
        known_width = float(request.form.get('known_width') or 0)
        known_length = float(request.form.get('known_length') or 0)
        perimeter_stone_width = float(request.form.get('perimeter_stone_width') or 1.0)
        cover_stone = float(request.form.get('cover_stone') or 1.0)
        base_stone = float(request.form.get('base_stone') or 0.333)
        stone_void = float(request.form.get('stone_void') or 0.40)
        pipe_connectors = int(request.form.get('pipe_connectors') or 0)
        top_adapters_12 = int(request.form.get('top_adapters_12') or 0)
        geo_waste = float(request.form.get('geoWaste') or 0)

        surface_elev = float(request.form.get('surface_elev') or 0) if request.form.get('surface_elev') else None
        tank_bottom_elev = float(request.form.get('tank_bottom_elev') or 0) if request.form.get('tank_bottom_elev') else None
        traffic_load = request.form.get('traffic_load', 'HS20')

        min_storage = float(request.form.get('min_storage') or 0) if request.form.get('min_storage') else None

        # Tank dimensions
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
        else:  # EX
            layer_heights = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
            void_ratio = 0.92633
            side_multiplier = 1.509186351

        tank_height = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]

        num_crates = crates_wide * crates_long * layers
        gross_tank_vol = num_crates * (MODULE_WID * MODULE_LEN * tank_height / layers)
        tank_storage = gross_tank_vol * void_ratio

        # Stone backfill envelope
        outer_width = tank_width + 2 * perimeter_stone_width
        outer_length = tank_length + 2 * perimeter_stone_width
        total_system_depth = base_stone + tank_height + cover_stone
        total_excavation_vol = outer_width * outer_length * total_system_depth

        # Estimated Stone Backfill Volume to Purchase
        stone_backfill_bulk_ft3 = total_excavation_vol - gross_tank_vol
        stone_backfill_bulk_yd3 = stone_backfill_bulk_ft3 / 27

        total_stone_storage = stone_backfill_bulk_ft3 * stone_void
        total_storage = tank_storage + total_stone_storage

        # Perimeter override
        tank_perimeter_override = request.form.get('tank_perimeter')
        if tank_perimeter_override and tank_perimeter_override.strip():
            tank_perimeter = float(tank_perimeter_override)
        else:
            tank_perimeter = 2 * (tank_width + tank_length)

        side_plates = round(tank_perimeter * (layers * side_multiplier) / 5.17)

        # BOM
        if config == 'SC':
            base_units = num_crates
            bottom_plates = crates_wide * crates_long
        else:
            base_units = num_crates * 2
            bottom_plates = 0

        # Burrito Geotextile
        tank_area = tank_width * tank_length
        tank_peri = 2 * (tank_width + tank_length)
        geo_tank = tank_area + (tank_peri * tank_height) + tank_area
        outer_area = outer_width * outer_length
        geo_stone = outer_area + (2 * (outer_width + outer_length) * total_system_depth) + outer_area
        waste_factor = max(geo_waste / 100.0, 0.0)
        geo_tank *= (1 + waste_factor)
        geo_stone *= (1 + waste_factor)
        geo_total = geo_tank + geo_stone

        # Elevation & Cover Logic
        tank_top_elev = None
        cover_depth = None
        cover_status = None
        if surface_elev is not None and tank_bottom_elev is not None:
            tank_top_elev = tank_bottom_elev + tank_height
            cover_depth = surface_elev - tank_top_elev
            min_cover_req = MIN_COVER[config].get(traffic_load, 1.0)
            cover_status = "PASS" if cover_depth >= min_cover_req else "FAIL"

        storage_status = "PASS" if min_storage is None or total_storage >= min_storage else "FAIL"

        results = {
            'config': config,
            'layers': layers,
            'known_width': known_width,
            'known_length': known_length,
            'tank_width': round(tank_width, 2),
            'tank_length': round(tank_length, 2),
            'tank_height': round(tank_height, 2),
            'tank_storage': round(tank_storage, 1),
            'stone_storage': round(total_stone_storage, 1),
            'total_storage': round(total_storage, 1),
            'base_units': base_units,
            'side_plates': side_plates,
            'bottom_plates': bottom_plates,
            'pipe_connectors': pipe_connectors,
            'top_adapters_12': top_adapters_12,
            'geoTank': round(geo_tank, 1),
            'geoStone': round(geo_stone, 1),
            'geoTotal': round(geo_total, 1),
            'geoWaste': geo_waste,
            'min_storage': round(min_storage, 1) if min_storage else None,
            'storage_status': storage_status,
            'used_perimeter': round(tank_perimeter, 2),
            'surface_elev': surface_elev,
            'tank_bottom_elev': tank_bottom_elev,
            'tank_top_elev': round(tank_top_elev, 2) if tank_top_elev is not None else None,
            'cover_depth': round(cover_depth, 2) if cover_depth is not None else None,
            'cover_status': cover_status,
            'traffic_load': traffic_load,
            'stone_backfill_bulk_ft3': round(stone_backfill_bulk_ft3, 1),
            'stone_backfill_bulk_yd3': round(stone_backfill_bulk_yd3, 2),
        }

        form_data = request.form

    return render_template('index.html', results=results, form_data=form_data)


@app.route('/download_pdf', methods=['POST'])
def download_pdf():
    # FULL RECALCULATION (identical to index route)
    project_name = request.form.get('project_name', 'Untitled Project')
    project_num = request.form.get('project_num', '')
    location = request.form.get('location', '')
    client = request.form.get('client', '')
    config = request.form.get('config', 'SC')
    layers = int(request.form.get('layers') or 3)
    known_width = float(request.form.get('known_width') or 0)
    known_length = float(request.form.get('known_length') or 0)
    perimeter_stone_width = float(request.form.get('perimeter_stone_width') or 1.0)
    cover_stone = float(request.form.get('cover_stone') or 1.0)
    base_stone = float(request.form.get('base_stone') or 0.333)
    stone_void = float(request.form.get('stone_void') or 0.40)
    pipe_connectors = int(request.form.get('pipe_connectors') or 0)
    top_adapters_12 = int(request.form.get('top_adapters_12') or 0)
    geo_waste = float(request.form.get('geoWaste') or 0)

    surface_elev = float(request.form.get('surface_elev') or 0) if request.form.get('surface_elev') else None
    tank_bottom_elev = float(request.form.get('tank_bottom_elev') or 0) if request.form.get('tank_bottom_elev') else None
    traffic_load = request.form.get('traffic_load', 'HS20')

    # Tank dimensions
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

    stone_backfill_bulk_ft3 = total_excavation_vol - gross_tank_vol
    stone_backfill_bulk_yd3 = stone_backfill_bulk_ft3 / 27

    total_stone_storage = stone_backfill_bulk_ft3 * stone_void
    total_storage = tank_storage + total_stone_storage

    tank_perimeter_override = request.form.get('tank_perimeter')
    if tank_perimeter_override and tank_perimeter_override.strip():
        tank_perimeter = float(tank_perimeter_override)
    else:
        tank_perimeter = 2 * (tank_width + tank_length)

    side_plates = round(tank_perimeter * (layers * side_multiplier) / 5.17)

    if config == 'SC':
        base_units = num_crates
        bottom_plates = crates_wide * crates_long
    else:
        base_units = num_crates * 2
        bottom_plates = 0

    # Burrito Geotextile
    tank_area = tank_width * tank_length
    tank_peri = 2 * (tank_width + tank_length)
    geo_tank = tank_area + (tank_peri * tank_height) + tank_area
    outer_area = outer_width * outer_length
    geo_stone = outer_area + (2 * (outer_width + outer_length) * total_system_depth) + outer_area
    waste_factor = max(geo_waste / 100.0, 0.0)
    geo_tank *= (1 + waste_factor)
    geo_stone *= (1 + waste_factor)
    geo_total = geo_tank + geo_stone

    # Elevation & Cover
    tank_top_elev = None
    cover_depth = None
    cover_status = None
    if surface_elev is not None and tank_bottom_elev is not None:
        tank_top_elev = tank_bottom_elev + tank_height
        cover_depth = surface_elev - tank_top_elev
        min_cover_req = MIN_COVER[config].get(traffic_load, 1.0)
        cover_status = "PASS" if cover_depth >= min_cover_req else "FAIL"

    # ====================== PDF GENERATION ======================
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    w, h = letter

    # LOGO (now using absolute path)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if os.path.exists(logo_path):
        c.drawImage(logo_path, 50, h - 100, width=180, height=60, preserveAspectRatio=True)

    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(w/2, h-180, "AquaCell V12 Crate Calculator")
    c.setFont("Helvetica", 11)
    c.drawCentredString(w/2, h-195, "Underground Stormwater Retention / Detention / Infiltration System")

    y = h - 230
    c.setFont("Helvetica", 10)

    lines = [
        f"Project Name: {project_name}",
        f"Configuration: {config} Configuration ({layers} Layers)",
        f"Surface Elevation: {surface_elev if surface_elev is not None else '—'} ft",
        f"Tank Bottom Elevation: {tank_bottom_elev if tank_bottom_elev is not None else '—'} ft",
        f"Tank Top Elevation: {tank_top_elev if tank_top_elev is not None else '—'} ft",
        f"Actual Cover Depth: {cover_depth:.2f} ft → {cover_status if cover_status else '—'}",
        f"Traffic Load: {traffic_load}",
        f"Snapped Tank: {tank_width:.2f} ft × {tank_length:.2f} ft × {tank_height:.2f} ft",
        f"AquaCell Tank Storage: {tank_storage:.1f} ft³",
        f"Stone Storage: {total_stone_storage:.1f} ft³",
        f"Total System Storage: {total_storage:.1f} ft³",
        f"Estimated Stone Backfill Volume to Purchase: {stone_backfill_bulk_ft3:.1f} ft³ ({stone_backfill_bulk_yd3:.2f} yd³)",
        "",
        "Bill of Materials",
        f"Base Unit (3091506) ................ {base_units}",
        f"Side Plate (2476600003) ............ {side_plates}",
        f"Bottom Plate (2476600001) .......... {bottom_plates}",
        f"8-12\" Pipe Connectors ............. {pipe_connectors}",
        f"12\" Top Adapters .................. {top_adapters_12}",
        "",
        "Geotextile Fabric (Burrito Wrap)",
        f"AquaCell Only ..................... {geo_tank:.1f} ft²",
        f"Stone Envelope .................... {geo_stone:.1f} ft²",
        f"Total Geotextile .................. {geo_total:.1f} ft²",
        f"Waste/Overlap ..................... {geo_waste}%",
    ]

    for line in lines:
        c.drawString(50, y, line)
        y -= 14

    # Disclaimer
    c.setFont("Helvetica", 8)
    disclaimer = "Disclaimer: This calculator provides preliminary, conceptual estimates only and is not a stamped engineering design. The Engineer of Record is solely responsible for final design and verification."
    from textwrap import wrap
    wrapped = wrap(disclaimer, width=110)
    y = 90
    for line in wrapped:
        c.drawString(50, y, line)
        y -= 10

    c.setFont("Helvetica", 8)
    c.drawString(50, 40, f"Generated {datetime.datetime.now().strftime('%m/%d/%Y %H:%M')}")

    c.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"AquaCell_{project_name or 'Project'}.pdf", mimetype='application/pdf')


if __name__ == '__main__':
    app.run(debug=True)
    
