from flask import Flask, render_template, request, send_file
import io
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import datetime
import math

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def index():
    results = None
    form_data = {}

    if request.method == 'POST':
        # ─────────────────────────────────────────────────────────────
        # READ ALL FORM DATA (matches your full index.html)
        # ─────────────────────────────────────────────────────────────
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
        geoWaste = int(request.form.get('geoWaste', 0) or 0)
        pipe_connectors = int(request.form.get('pipe_connectors', 0) or 0)
        top_adapters_12 = int(request.form.get('top_adapters_12', 0) or 0)
        project_notes = request.form.get('project_notes', '')
        include_stage_storage = request.form.get('include_stage_storage') == 'yes'
        stage_increment_in = int(request.form.get('stage_increment_in', 12) or 12)

        # ─────────────────────────────────────────────────────────────
        # CALCULATIONS - FULL SC/EX SUPPORT
        # ─────────────────────────────────────────────────────────────
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
        else:  # EX Configuration
            layer_heights = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
            void_ratio = 0.92633
            side_multiplier = 1.509186351

        tank_height = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]

        num_crates = crates_wide * crates_long * layers
        gross_tank_vol = num_crates * (MODULE_WID * MODULE_LEN * tank_height / layers)
        tank_storage = gross_tank_vol * void_ratio

        # Stone backfill
        outer_width = tank_width + 2 * perimeter_stone_width
        outer_length = tank_length + 2 * perimeter_stone_width
        total_system_depth = base_stone + tank_height + cover_stone
        total_excavation_vol = outer_width * outer_length * total_system_depth
        total_stone_storage = (total_excavation_vol - gross_tank_vol) * stone_void
        total_storage = tank_storage + total_stone_storage

        # Side plates
        tank_perim_calc = float(tank_perimeter) if tank_perimeter else 2 * (tank_width + tank_length)
        side_plates = round(tank_perim_calc * (layers * side_multiplier) / 5.17)

        # BOM
        if config == 'SC':
            base_units = num_crates
            bottom_plates = crates_wide * crates_long
        else:
            base_units = num_crates * 2
            bottom_plates = 0

        # Stage Storage Table
        stage_storage = None
        if include_stage_storage:
            stage_storage = []
            increment_ft = stage_increment_in / 12.0
            current_elev = tank_bottom_elev
            while current_elev <= surface_elev + 0.01:
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

        results = {
            'config': config,
            'layers': layers,
            'surface_elev': surface_elev,
            'tank_bottom_elev': tank_bottom_elev,
            'tank_top_elev': round(tank_bottom_elev + tank_height, 2),
            'cover_depth': round(surface_elev - (tank_bottom_elev + tank_height), 2),
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
            'project_notes': project_notes,
            'stage_storage': stage_storage,
            'stage_increment_in': stage_increment_in,
            'min_storage': min_storage if min_storage > 0 else None,
            'storage_status': 'PASS' if min_storage <= total_storage else 'FAIL' if min_storage > 0 else None,
        }

        form_data = request.form

    return render_template('index.html', results=results, form_data=form_data)

@app.route('/download_pdf', methods=['POST'])
def download_pdf():
    # Full re-calculation for PDF (exact same as index route)
    project_name = request.form.get('project_name', 'Project')
    config = request.form.get('config', 'SC')
    layers = int(request.form.get('layers', 3))
    known_width = float(request.form.get('known_width', 0))
    known_length = float(request.form.get('known_length', 0))
    perimeter_stone_width = float(request.form.get('perimeter_stone_width', 1.0))
    cover_stone = float(request.form.get('cover_stone', 1.0))
    base_stone = float(request.form.get('base_stone', 0.333))
    stone_void = float(request.form.get('stone_void', 0.40))
    pipe_connectors = int(request.form.get('pipe_connectors', 0))
    top_adapters_12 = int(request.form.get('top_adapters_12', 0))
    total_aquacell_cost = request.form.get('totalAquaCellCost', '—')

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

    tank_perimeter = 2 * (tank_width + tank_length)
    side_plates = round(tank_perimeter * (layers * side_multiplier) / 5.17)

    if config == 'SC':
        base_units = num_crates
        bottom_plates = crates_wide * crates_long
    else:
        base_units = num_crates * 2
        bottom_plates = 0

    # Build PDF
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    w, h = letter
    y = h - 50

    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, h - 80, "AquaCell V12 Crate Calculator")
    c.setFont("Helvetica", 12)
    c.drawString(50, h - 110, f"Project: {project_name} | Config: {config} | Layers: {layers}")

    lines = [
        f"Snapped Tank: {round(tank_width,2)} ft × {round(tank_length,2)} ft × {round(tank_height,2)} ft",
        f"AquaCell Tank Storage: {round(tank_storage,1)} ft³",
        f"Stone Storage: {round(total_stone_storage,1)} ft³",
        f"Total System Storage: {round(total_storage,1)} ft³",
        f"Total AquaCell Cost: {total_aquacell_cost}",
        "",
        "Bill of Materials",
        f"Base Units ................ {base_units}",
        f"Side Plates ............... {side_plates}",
        f"Bottom Plates ............. {bottom_plates}",
        f"Pipe Connectors ........... {pipe_connectors}",
        f"Top Adapters .............. {top_adapters_12}",
    ]

    y = h - 150
    for line in lines:
        c.drawString(50, y, line)
        y -= 22

    c.setFont("Helvetica", 9)
    c.drawString(50, 50, f"Generated {datetime.datetime.now().strftime('%m/%d/%Y %H:%M')}")
    c.save()

    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="AquaCell_V12_Report.pdf", mimetype='application/pdf')

if __name__ == '__main__':
    app.run(debug=True)
