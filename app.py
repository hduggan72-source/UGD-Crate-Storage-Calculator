from flask import Flask, render_template, request, send_file
import io
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import datetime
import math

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Clean input handling - no leftover test project data
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

        MODULE_WID = 1.9685
        MODULE_LEN = 3.937
        crates_wide = math.floor(known_width / MODULE_WID)
        crates_long = math.floor(known_length / MODULE_LEN)
        tank_width = crates_wide * MODULE_WID
        tank_length = crates_long * MODULE_LEN

        if config == 'SC':
            layer_heights = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
            void_ratio = 0.95486
            bottom_plates_per_unit = 1
        else:
            layer_heights = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
            void_ratio = 0.92633
            bottom_plates_per_unit = 0

        tank_height = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]

        num_crates = crates_wide * crates_long * layers
        gross_tank_vol = num_crates * (MODULE_WID * MODULE_LEN * tank_height / layers)
        tank_storage = gross_tank_vol * void_ratio

        # Correct stone backfill calculation
        outer_width = tank_width + 2 * perimeter_stone_width
        outer_length = tank_length + 2 * perimeter_stone_width
        total_system_depth = base_stone + tank_height + cover_stone
        total_excavation_vol = outer_width * outer_length * total_system_depth
        total_stone_storage = (total_excavation_vol - gross_tank_vol) * stone_void

        total_storage = tank_storage + total_stone_storage

        # Side plates (your exact formula)
        tank_perimeter = 2 * (tank_width + tank_length)
        side_plates = round(tank_perimeter * (layers * 1.312336) / 5.17)

        base_units = num_crates
        bottom_plates = crates_wide * crates_long if config == 'SC' else 0

        results = {
            'known_width': known_width,
            'known_length': known_length,
            'tank_width': round(tank_width, 2),
            'tank_length': round(tank_length, 2),
            'tank_height': round(tank_height, 2),
            'tank_storage': round(tank_storage, 1),
            'total_stone_storage': round(total_stone_storage, 1),
            'total_storage': round(total_storage, 1),
            'base_units': int(base_units),
            'side_plates': int(side_plates),
            'bottom_plates': int(bottom_plates),
            'pipe_connectors': pipe_connectors,
            'top_adapters_12': top_adapters_12,
            'config': config,
            'layers': layers,
            'date': datetime.date.today().strftime('%m/%d/%Y')
        }

        return render_template('index.html', results=results, form_data=request.form)

    # First load = completely clean / fresh form
    return render_template('index.html', form_data={})

@app.route('/download_pdf', methods=['POST'])
def download_pdf():
    # Read all form data
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
    total_aquacell_cost = request.form.get('totalAquaCellCost', '—')

    # Recalculate everything
    MODULE_WID = 1.9685
    MODULE_LEN = 3.937
    crates_wide = math.floor(known_width / MODULE_WID)
    crates_long = math.floor(known_length / MODULE_LEN)
    tank_width = crates_wide * MODULE_WID
    tank_length = crates_long * MODULE_LEN

    if config == 'SC':
        layer_heights = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
        void_ratio = 0.95486
        bottom_plates_per_unit = 1
    else:
        layer_heights = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
        void_ratio = 0.92633
        bottom_plates_per_unit = 0

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
    side_plates = round(tank_perimeter * (layers * 1.312336) / 5.17)

    base_units = num_crates
    bottom_plates = crates_wide * crates_long if config == 'SC' else 0

    # ─────────────────────────────────────────────────────────────
    # BUILD PDF - LOGO ABOVE HEADER (Larger & Cleaner)
    # ─────────────────────────────────────────────────────────────
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    w, h = letter
    y = h - 50

         # Logo centered at the very top - EVEN BIGGER (final size)
    try:
        import os
        from reportlab.lib.utils import ImageReader
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wavin-logo.png')
        if os.path.exists(logo_path):
            logo = ImageReader(logo_path)
            c.drawImage(logo, 146, h - 172, width=320, height=135, preserveAspectRatio=True, mask='auto')
            print("✅ Logo loaded successfully (large & centered)")
        else:
            raise Exception("File not found")
    except Exception as e:
        print(f"❌ Logo failed: {e}")
        c.setFont("Helvetica-Bold", 18)
        c.drawString(50, h - 95, "WAVIN")

    # Title text BELOW the larger logo
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, h - 205, "AquaCell v.12 Crate Calculator")
    c.setFont("Helvetica", 14)
    c.drawString(50, h - 225, "Underground Stormwater System")
    y = h - 265   # extra space after logo + title

    c.setFont("Helvetica", 12)
    lines = [
        f"Project Name: {project_name}",
        f"Project Num: {project_num}",
        f"Location: {location}",
        f"Client: {client}",
        f"Config: {config} — {layers} Layers",
        f"Known Site Dimensions: {known_width} ft × {known_length} ft",
        f"Snapped Tank Dimensions: {round(tank_width,2)} ft × {round(tank_length,2)} ft × {round(tank_height,2)} ft",
        f"AquaCell Tank Storage: {round(tank_storage,1)} ft³",
        f"Stone Backfill Storage: {round(total_stone_storage,1)} ft³",
        f"Total System Storage: {round(total_storage,1)} ft³",
        f"Total AquaCell Cost (USD$): {total_aquacell_cost}",
        "",
        "Bill of Materials",
        f"Base Units ................ {base_units}",
        f"Side Plates ............... {side_plates}",
        f"Bottom Plates ............. {bottom_plates}",
        f"8-12\" Pipe Connectors ... {pipe_connectors}",
        f"12\" Top Adapters .......... {top_adapters_12}",
    ]

    for line in lines:
        c.drawString(50, y, line)
        y -= 20

    # Disclaimer
    c.setFont("Helvetica", 9)
    disclaimer = (
        "Disclaimer: This calculator provides preliminary, conceptual estimates only and is not a stamped engineering "
        "design. Wavin’s assistance in sizing or product selection is advisory and does not constitute design responsibility "
        "or guarantee system performance. The Engineer of Record (EoR) is solely responsible for verifying all design."
    )
    from textwrap import wrap
    wrapped = wrap(disclaimer, width=110)
    y = 110
    for line in wrapped:
        c.drawString(50, y, line)
        y -= 12

    c.setFont("Helvetica", 9)
    c.drawString(50, 40, f"Generated {datetime.datetime.now().strftime('%m/%d/%Y %H:%M')} | Polypropylene Crates for Retention/Detention")
    c.save()

    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="AquaCell_V12_Report.pdf", mimetype='application/pdf')

if __name__ == '__main__':
    app.run(debug=True)
    
