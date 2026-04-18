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
        # Read form data
        project_name = request.form.get('project_name', '')
        project_num = request.form.get('project_num', '')
        location = request.form.get('location', '')
        client = request.form.get('client', '')
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
        geo_waste = float(request.form.get('geoWaste', 0))

        # Calculations
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

        # Stone backfill
        outer_width = tank_width + 2 * perimeter_stone_width
        outer_length = tank_length + 2 * perimeter_stone_width
        total_system_depth = base_stone + tank_height + cover_stone
        total_excavation_vol = outer_width * outer_length * total_system_depth
        total_stone_storage = (total_excavation_vol - gross_tank_vol) * stone_void
        total_storage = tank_storage + total_stone_storage

        # Side plates
        tank_perimeter = 2 * (tank_width + tank_length)
        side_plates = round(tank_perimeter * (layers * side_multiplier) / 5.17)

        # BOM
        if config == 'SC':
            base_units = num_crates
            bottom_plates = crates_wide * crates_long
        else:  # EX
            base_units = num_crates * 2
            bottom_plates = 0

        # ─────────────────────────────────────────────────────────────
        # GEOTEXTILE - BURRITO STYLE (Always Wrapped)
        # ─────────────────────────────────────────────────────────────
        tank_area = tank_width * tank_length
        tank_peri = 2 * (tank_width + tank_length)
        # AquaCell tank: top + sides + bottom (always wrapped)
        geo_tank = tank_area + (tank_peri * tank_height) + tank_area

        # Stone envelope: full outer system including bottom of base stone (always wrapped)
        outer_area = outer_width * outer_length
        geo_stone = outer_area + (2 * (outer_width + outer_length) * total_system_depth) + outer_area

        # Waste factor
        waste_factor = max(geo_waste / 100.0, 0.0)
        geo_tank *= (1 + waste_factor)
        geo_stone *= (1 + waste_factor)
        geo_total = geo_tank + geo_stone

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
            # Geotextile
            'geoTank': round(geo_tank, 1),
            'geoStone': round(geo_stone, 1),
            'geoTotal': round(geo_total, 1),
            'geoWaste': geo_waste,
        }

        form_data = request.form

    return render_template('index.html', results=results, form_data=form_data)

@app.route('/download_pdf', methods=['POST'])
def download_pdf():
    # Read all form data
    project_name = request.form.get('project_name', 'Untitled Project')
    project_num = request.form.get('project_num', '')
    location = request.form.get('location', '')
    client = request.form.get('client', '')
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
    geo_waste = float(request.form.get('geoWaste', 0))
    total_aquacell_cost = request.form.get('totalAquaCellCost', '—')

    # ─────────────────────────────────────────────────────────────
    # RECALCULATE EVERYTHING (including burrito geotextile)
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
    else:  # EX
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

    # Burrito-style Geotextile (always wrapped)
    tank_area = tank_width * tank_length
    tank_peri = 2 * (tank_width + tank_length)
    geo_tank = tank_area + (tank_peri * tank_height) + tank_area   # top + sides + bottom

    outer_area = outer_width * outer_length
    geo_stone = outer_area + (2 * (outer_width + outer_length) * total_system_depth) + outer_area

    waste_factor = max(geo_waste / 100.0, 0.0)
    geo_tank *= (1 + waste_factor)
    geo_stone *= (1 + waste_factor)
    geo_total = geo_tank + geo_stone

    # ─────────────────────────────────────────────────────────────
    # BUILD PDF - Smaller fonts + Logo + Full Disclaimer
    # ─────────────────────────────────────────────────────────────
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    w, h = letter
    y = h - 80

    # Wavin Logo (centered at top)
    try:
        from reportlab.lib.utils import ImageReader
        import os
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wavin-logo.png')
        if os.path.exists(logo_path):
            logo = ImageReader(logo_path)
            c.drawImage(logo, (w-320)/2, h-160, width=320, height=100, preserveAspectRatio=True)
    except:
        pass  # fallback if logo missing

    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(w/2, h-180, "AquaCell V12 Crate Calculator")
    c.setFont("Helvetica", 11)
    c.drawCentredString(w/2, h-195, "Underground Stormwater Retention / Detention System")

    y = h - 230

    c.setFont("Helvetica", 10)
    lines = [
        f"Project Name: {project_name}",
        f"Project Num: {project_num}",
        f"Location: {location}",
        f"Client: {client}",
        f"Config: {config} — {layers} Layers",
        f"Known Site Dimensions: {known_width:.1f} ft × {known_length:.1f} ft",
        f"Snapped Tank: {tank_width:.2f} ft × {tank_length:.2f} ft × {tank_height:.2f} ft",
        f"AquaCell Tank Storage: {tank_storage:.1f} ft³",
        f"Stone Storage: {total_stone_storage:.1f} ft³",
        f"Total System Storage: {total_storage:.1f} ft³",
        "",
        "Geotextile Fabric (Min. 6 oz/yd² Non-woven geotextile)",
        f"Geotextile - AquaCell Only: {geo_tank:.1f} ft²",
        f"Geotextile - Stone Envelope: {geo_stone:.1f} ft²",
        f"Total Geotextile: {geo_total:.1f} ft²",
        f"Waste/Overlap: {geo_waste:.1f}%",
        "",
        "Bill of Materials",
        f"Base Unit (3091506) ................ {base_units}",
        f"Side Plate (2476600003) ............ {side_plates}",
        f"Bottom Plate (2476600001) .......... {bottom_plates}",
        f"8-12\" Pipe Connectors ............. {pipe_connectors}",
        f"12\" Top Adapters .................. {top_adapters_12}",
        "",
        f"Total AquaCell Cost (USD$): {total_aquacell_cost}",
    ]

    for line in lines:
        c.drawString(50, y, line)
        y -= 14   # tighter line spacing

    # Full Disclaimer at bottom
    c.setFont("Helvetica", 8)
    disclaimer = (
        "Disclaimer: This calculator provides preliminary, conceptual estimates only and is not a stamped engineering "
        "design. Wavin’s assistance is advisory only. The Engineer of Record (EoR) is solely responsible for verifying "
        "all design, calculations, and site-specific conditions including soil bearing capacity, compaction, "
        "geotextile selection, and structural performance."
    )
    from textwrap import wrap
    wrapped = wrap(disclaimer, width=110)
    y = 90
    for line in wrapped:
        c.drawString(50, y, line)
        y -= 10

    c.setFont("Helvetica", 8)
    c.drawString(50, 40, f"Generated {datetime.datetime.now().strftime('%m/%d/%Y %H:%M')} | Polypropylene Crates for Retention/Detention/Infiltration")

    c.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="AquaCell_V12_Report.pdf", mimetype='application/pdf')

if __name__ == '__main__':
    app.run(debug=True)
