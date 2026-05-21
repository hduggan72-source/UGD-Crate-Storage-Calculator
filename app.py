from flask import Flask, render_template, request, send_file
import io
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
import datetime
import math
import os
import base64
from io import BytesIO

app = Flask(__name__)

# ── ASTM F2787 Live Load Model constants ──
_LL_m    = 1.2
_LL_IM   = 0.2475
_LL_LLDF = 1.15

def _ll_wheel(traffic_load):
    return {'H10': (8000, 10, 10), 'HS20': (16000, 10, 20), 'HS25': (20000, 10, 20)}.get(traffic_load, (16000, 10, 20))

def calc_live_load_fos(traffic_load, cover_depth_ft, config):
    if cover_depth_ft <= 0:
        return None, None, None, None
    cover_in = cover_depth_ft * 12
    wl, tire_L, tire_W = _ll_wheel(traffic_load)
    proj_area_in2 = (tire_L + cover_in * _LL_LLDF) * (tire_W + cover_in * _LL_LLDF)
    ll_lbs = wl * _LL_m * (1 + _LL_IM) + wl / 180.0
    ll_psi = ll_lbs / proj_area_in2
    dl_psi = (cover_in / 12.0) * 120 / 144
    max_str = 70 if config == 'SC' else 100
    fos_ll = round(max_str / (ll_psi + dl_psi), 2) if (ll_psi + dl_psi) > 0 else None
    fos_dl = round(max_str / dl_psi, 2) if dl_psi > 0 else None
    return round(ll_psi, 3), round(dl_psi, 3), fos_ll, fos_dl

@app.route('/', methods=['GET', 'POST'])
def index():
    results = None
    form_data = {}

    if request.method == 'POST':
        project_name = request.form.get('project_name', '')
        project_num  = request.form.get('project_num', '')
        location     = request.form.get('location', '')
        client       = request.form.get('client', '')
        config       = request.form.get('config', 'SC')
        layers       = int(request.form.get('layers', 3))
        surface_elev      = float(request.form.get('surface_elev', 0) or 0)
        tank_bottom_elev  = float(request.form.get('tank_bottom_elev', 0) or 0)
        traffic_load      = request.form.get('traffic_load', 'HS20')
        known_width       = float(request.form.get('known_width', 0) or 0)
        known_length      = float(request.form.get('known_length', 0) or 0)
        tank_perimeter    = request.form.get('tank_perimeter', '').strip()
        perimeter_stone_width = float(request.form.get('perimeter_stone_width', 1.0) or 1.0)
        cover_stone       = float(request.form.get('cover_stone', 1.0) or 1.0)
        base_stone        = float(request.form.get('base_stone', 0.333) or 0.333)
        min_storage       = float(request.form.get('min_storage', 0) or 0)
        stone_void        = float(request.form.get('stone_void', 0.40) or 0.40)
        geoWaste          = int(request.form.get('geoWaste', 10) or 10)
        pipe_connectors   = int(request.form.get('pipe_connectors', 0) or 0)
        top_adapters_12   = int(request.form.get('top_adapters_12', 0) or 0)
        top_adapters_16   = int(request.form.get('top_adapters_16', 0) or 0)
        project_notes     = request.form.get('project_notes', '')
        include_stage_storage = request.form.get('include_stage_storage') == 'yes'
        include_schematic     = request.form.get('include_schematic') == 'yes'
        stage_increment_in    = int(request.form.get('stage_increment_in', 12) or 12)
        shape_mode            = request.form.get('shape_mode', 'rectangle')

        MODULE_WID = 1.9685
        MODULE_LEN = 3.937

        if config == 'SC':
            layer_heights   = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
            void_ratio      = 0.95486
            side_multiplier = 1.312336
        else:
            layer_heights   = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
            void_ratio      = 0.92633
            side_multiplier = 1.509186351

        tank_height        = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]
        total_system_depth = base_stone + tank_height + cover_stone

        if shape_mode == 'complex':
            complex_scaled_area = float(request.form.get('complex_scaled_area', 0) or 0)
            complex_tank_perim  = float(request.form.get('complex_tank_perim', 0) or 0)
            complex_known_dim   = float(request.form.get('complex_known_dim', 0) or 0)
            complex_excav_area  = float(request.form.get('complex_excav_area', 0) or 0)
            complex_excav_perim = float(request.form.get('complex_excav_perim', 0) or 0)
            other_dim    = complex_scaled_area / complex_known_dim if complex_known_dim > 0 else 0
            crates_known = math.floor(complex_known_dim / MODULE_WID) if complex_known_dim > 0 else 0
            crates_other = math.floor(other_dim / MODULE_LEN) if other_dim > 0 else 0
            snapped_known = crates_known * MODULE_WID
            snapped_other = crates_other * MODULE_LEN
            complex_tank_area = snapped_known * snapped_other
            crates_per_layer  = crates_known * crates_other
            num_crates        = crates_per_layer * layers
            gross_tank_vol    = complex_tank_area * tank_height
            tank_storage      = gross_tank_vol * void_ratio
            tank_perim_calc   = complex_tank_perim
            side_plates       = round(tank_perim_calc * (layers * side_multiplier) / 5.17)
            if config == 'SC':
                base_units    = num_crates
                bottom_plates = crates_per_layer
            else:
                base_units    = num_crates * 2
                bottom_plates = 0
            if complex_excav_area <= 0:
                complex_excav_area = (snapped_known + 2*perimeter_stone_width) * (snapped_other + 2*perimeter_stone_width)
            if complex_excav_perim <= 0:
                complex_excav_perim = complex_tank_perim + 8*perimeter_stone_width
            total_excavation_vol  = complex_excav_area * total_system_depth
            stone_envelope_volume = total_excavation_vol - gross_tank_vol
            total_stone_storage   = stone_envelope_volume * stone_void
            total_storage         = tank_storage + total_stone_storage
            geoTank  = round((2*complex_tank_area + complex_tank_perim*tank_height) * (1 + geoWaste/100.0), 1)
            geoStone = round((2*complex_excav_area + complex_excav_perim*total_system_depth) * (1 + geoWaste/100.0), 1)
            geoTotal = round(geoTank + geoStone, 1)
            used_perimeter = round(complex_tank_perim, 2)
            tank_width     = round(snapped_known, 2)
            tank_length    = round(snapped_other, 2)
        else:
            crates_wide = math.floor(known_width / MODULE_WID)
            crates_long = math.floor(known_length / MODULE_LEN)
            tank_width  = crates_wide * MODULE_WID
            tank_length = crates_long * MODULE_LEN
            num_crates     = crates_wide * crates_long * layers
            gross_tank_vol = num_crates * (MODULE_WID * MODULE_LEN * tank_height / layers)
            tank_storage   = gross_tank_vol * void_ratio
            outer_width  = tank_width  + 2*perimeter_stone_width
            outer_length = tank_length + 2*perimeter_stone_width
            total_excavation_vol  = outer_width * outer_length * total_system_depth
            stone_envelope_volume = total_excavation_vol - gross_tank_vol
            total_stone_storage   = stone_envelope_volume * stone_void
            total_storage         = tank_storage + total_stone_storage
            used_perimeter  = 2 * (tank_width + tank_length)
            tank_perim_calc = float(tank_perimeter) if tank_perimeter else used_perimeter
            side_plates     = round(tank_perim_calc * (layers * side_multiplier) / 5.17)
            if config == 'SC':
                base_units    = num_crates
                bottom_plates = crates_wide * crates_long
            else:
                base_units    = num_crates * 2
                bottom_plates = 0
            tank_top_bottom_area = 2 * tank_width * tank_length
            tank_sides_area      = used_perimeter * tank_height
            geoTank  = round((tank_top_bottom_area + tank_sides_area) * (1 + geoWaste/100.0), 1)
            geoStone = round((outer_width*outer_length*2 + outer_width*total_system_depth*2 + outer_length*total_system_depth*2) * (1 + geoWaste/100.0), 1)
            geoTotal = round(geoTank + geoStone, 1)
            complex_tank_area = complex_tank_perim = complex_excav_area = complex_excav_perim = None

        stone_backfill_bulk_ft3  = round(stone_envelope_volume * 1.10, 1)
        stone_backfill_bulk_yd3  = round(stone_backfill_bulk_ft3 / 27, 2)
        stone_backfill_bulk_tons = round(stone_backfill_bulk_ft3 * 100 / 2000, 2)

        if shape_mode == 'complex':
            excav_area_for_layers = complex_excav_area
            tank_footprint        = complex_tank_area
        else:
            excav_area_for_layers = outer_width * outer_length
            tank_footprint        = tank_width * tank_length

        stone_top_gross      = round(excav_area_for_layers * cover_stone, 1)
        stone_top_net        = round(stone_top_gross * stone_void, 1)
        stone_perim_gross    = round((excav_area_for_layers - tank_footprint) * tank_height, 1)
        stone_perim_net      = round(stone_perim_gross * stone_void, 1)
        stone_base_gross     = round(excav_area_for_layers * base_stone, 1)
        stone_base_net       = round(stone_base_gross * stone_void, 1)
        stone_layer_total_gross = round(stone_top_gross + stone_perim_gross + stone_base_gross, 1)
        stone_layer_total_net   = round(stone_top_net + stone_perim_net + stone_base_net, 1)

        stage_storage = None
        if include_stage_storage:
            stage_storage = []
            increment_ft = stage_increment_in / 12.0
            top_of_stone = tank_bottom_elev + tank_height + cover_stone
            current_elev = tank_bottom_elev - base_stone
            while current_elev <= top_of_stone + 0.01:
                depth_in_tank  = max(0, min(tank_height, current_elev - tank_bottom_elev))
                tank_vol_at_elev = (depth_in_tank / tank_height) * tank_storage if tank_height > 0 else 0
                depth_in_stone = max(0, min(total_system_depth, current_elev - (tank_bottom_elev - base_stone)))
                stone_vol_at_elev = (depth_in_stone / total_system_depth) * total_stone_storage if total_system_depth > 0 else 0
                stage_storage.append({
                    'elevation_ft': round(current_elev, 2),
                    'tank_storage':  round(tank_vol_at_elev, 1),
                    'stone_storage': round(stone_vol_at_elev, 1),
                    'total_storage': round(tank_vol_at_elev + stone_vol_at_elev, 1)
                })
                current_elev += increment_ft

        cover_depth = round(surface_elev - (tank_bottom_elev + tank_height), 2)
        if traffic_load == 'H10':
            min_cover_req = 1.0
        elif traffic_load == 'HS20':
            min_cover_req = 1.5 if config == 'SC' else 1.33
        elif traffic_load == 'HS25':
            min_cover_req = 2.5 if config == 'SC' else 1.83
        else:
            min_cover_req = 1.0

        max_cover_req  = 14.4 if config == 'SC' else 26.2
        cover_status   = 'PASS' if cover_depth >= min_cover_req else 'FAIL'
        dead_load_psi  = round(cover_stone * 120 / 144, 2)
        max_compressive = 70 if config == 'SC' else 100
        fos_dead       = round(max_compressive / dead_load_psi, 2) if dead_load_psi > 0 else None
        ll_psi, dl_psi_check, fos_live_load, fos_dead_check = calc_live_load_fos(traffic_load, cover_depth, config)
        storage_status = 'PASS' if min_storage <= total_storage else 'FAIL' if min_storage > 0 else None

        results = {
            'config': config, 'layers': layers,
            'surface_elev': surface_elev, 'tank_bottom_elev': tank_bottom_elev,
            'tank_top_elev': round(tank_bottom_elev + tank_height, 2),
            'cover_depth': cover_depth, 'cover_status': cover_status,
            'max_cover_req': max_cover_req,
            'max_cover_status': 'PASS' if cover_depth <= max_cover_req else 'FAIL',
            'dead_load_psi': dead_load_psi, 'fos_dead': fos_dead,
            'll_psi': ll_psi, 'fos_live_load': fos_live_load, 'traffic_load': traffic_load,
            'tank_width': round(tank_width, 2), 'tank_length': round(tank_length, 2),
            'tank_height': round(tank_height, 2),
            'tank_storage': round(tank_storage, 1),
            'stone_storage': round(total_stone_storage, 1),
            'total_storage': round(total_storage, 1),
            'shape_mode': shape_mode,
            'complex_scaled_area':  complex_scaled_area  if shape_mode == 'complex' else None,
            'complex_tank_area':    round(complex_tank_area, 2) if shape_mode == 'complex' else None,
            'complex_tank_perim':   complex_tank_perim   if shape_mode == 'complex' else None,
            'complex_known_dim':    complex_known_dim    if shape_mode == 'complex' else None,
            'complex_snapped_known':round(snapped_known, 3) if shape_mode == 'complex' else None,
            'complex_snapped_other':round(snapped_other, 3) if shape_mode == 'complex' else None,
            'complex_crates_known': crates_known         if shape_mode == 'complex' else None,
            'complex_crates_other': crates_other         if shape_mode == 'complex' else None,
            'complex_excav_area':   round(complex_excav_area, 1)  if shape_mode == 'complex' else None,
            'complex_excav_perim':  round(complex_excav_perim, 1) if shape_mode == 'complex' else None,
            'used_perimeter': round(used_perimeter, 2),
            'base_units': base_units, 'side_plates': side_plates,
            'bottom_plates': bottom_plates, 'pipe_connectors': pipe_connectors,
            'top_adapters_12': top_adapters_12, 'top_adapters_16': top_adapters_16,
            'project_notes': project_notes,
            'stage_storage': stage_storage, 'stage_increment_in': stage_increment_in,
            'min_storage': min_storage if min_storage > 0 else None,
            'storage_status': storage_status,
            'stone_backfill_bulk_ft3': stone_backfill_bulk_ft3,
            'stone_backfill_bulk_yd3': stone_backfill_bulk_yd3,
            'stone_backfill_bulk_tons': stone_backfill_bulk_tons,
            'stone_top_gross': stone_top_gross, 'stone_top_net': stone_top_net,
            'stone_perim_gross': stone_perim_gross, 'stone_perim_net': stone_perim_net,
            'stone_base_gross': stone_base_gross, 'stone_base_net': stone_base_net,
            'stone_layer_total_gross': stone_layer_total_gross,
            'stone_layer_total_net':   stone_layer_total_net,
            'geoTank': geoTank, 'geoStone': geoStone, 'geoTotal': geoTotal,
            'geoWaste': geoWaste, 'include_schematic': include_schematic,
        }
        form_data = request.form

    return render_template('index.html', results=results, form_data=form_data)


# ══════════════════════════════════════════════════════════════════
#  COLOR PALETTE & LAYOUT CONSTANTS
# ══════════════════════════════════════════════════════════════════
NAVY   = colors.HexColor('#0f1e3c')
BLUE   = colors.HexColor('#1e3a8a')
DKBLUE = colors.HexColor('#1e40af')
LTBLUE = colors.HexColor('#dbeafe')
GREEN  = colors.HexColor('#15803d')
LTGRN  = colors.HexColor('#dcfce7')
RED    = colors.HexColor('#b91c1c')
GRAY   = colors.HexColor('#64748b')
LGRAY  = colors.HexColor('#f1f5f9')
MGRAY  = colors.HexColor('#e2e8f0')
AMBER  = colors.HexColor('#92400e')
LTAMB  = colors.HexColor('#fef3c7')
WHITE  = colors.white
BLACK  = colors.black

LM = 45           # left margin
RM = 45           # right margin
PW, PH = letter   # 612 x 792
CW = PW - LM - RM # usable content width = 522


# ══════════════════════════════════════════════════════════════════
#  PDF DRAWING HELPERS
# ══════════════════════════════════════════════════════════════════

def _rule(c, y, lm=LM, rw=CW, thickness=0.4, clr=MGRAY):
    c.setStrokeColor(clr)
    c.setLineWidth(thickness)
    c.line(lm, y, lm + rw, y)

def _section_header(c, y, title):
    """Navy band section label. Returns y below band."""
    band_h = 16
    c.setFillColor(NAVY)
    c.rect(LM, y - band_h + 4, CW, band_h, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 8.5)
    c.drawString(LM + 6, y - band_h + 8, title)
    return y - band_h - 2

def _sub_label(c, y, text):
    """Small all-caps group label (e.g. ELEVATIONS). Returns y below."""
    c.setFillColor(GRAY)
    c.setFont('Helvetica-Bold', 6.5)
    c.drawString(LM + 5, y - 3, text)
    return y - 11

def _kv_row(c, y, label, value, val_color=BLACK, shade=False):
    """Single label / value row. Returns y below."""
    rh = 13
    if shade:
        c.setFillColor(LGRAY)
        c.rect(LM, y - rh + 3, CW, rh, fill=1, stroke=0)
    c.setFillColor(GRAY)
    c.setFont('Helvetica', 7.5)
    c.drawString(LM + 5, y - 3, label)
    c.setFillColor(val_color)
    c.setFont('Helvetica-Bold', 7.5)
    c.drawRightString(LM + CW - 5, y - 3, str(value))
    _rule(c, y - rh + 3, thickness=0.25)
    return y - rh

def _kv_row_colored(c, y, label, value, status, shade=False):
    """
    Like _kv_row but value text is GREEN for PASS, RED for FAIL, BLACK otherwise.
    No badge — just colored text.
    """
    col = GREEN if status == 'PASS' else (RED if status == 'FAIL' else BLACK)
    return _kv_row(c, y, label, value, val_color=col, shade=shade)

def _highlight_row(c, y, label, value, bg=LTBLUE, fg=BLUE, label_color=None):
    """Full-width highlighted row (e.g. Total Storage). Returns y below."""
    rh = 16
    c.setFillColor(bg)
    c.rect(LM, y - rh + 3, CW, rh, fill=1, stroke=0)
    c.setFillColor(label_color if label_color else fg)
    c.setFont('Helvetica-Bold', 8.5)
    c.drawString(LM + 5, y - 3, label)
    c.setFillColor(fg)
    c.setFont('Helvetica-Bold', 8.5)
    c.drawRightString(LM + CW - 5, y - 3, str(value))
    _rule(c, y - rh + 3, thickness=0.25)
    return y - rh

def _table_header(c, y, cols):
    """
    cols: list of (label, x_abs, col_width, align)
    Draws a DKBLUE band spanning full CW, writes headers. Returns y below.
    """
    rh = 14
    # Always span the full content width from LM
    c.setFillColor(BLUE)
    c.rect(LM, y - rh + 3, CW, rh, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 7.5)
    for label, x_abs, col_w, align in cols:
        if align == 'right':
            c.drawRightString(x_abs + col_w, y - 3, label)
        else:
            c.drawString(x_abs, y - 3, label)
    return y - rh

def _table_row(c, y, cells, shade=False):
    """
    cells: list of (text, x_abs, col_width, align, bold, color)
    """
    rh = 13
    if shade:
        c.setFillColor(LGRAY)
        c.rect(LM, y - rh + 3, CW, rh, fill=1, stroke=0)
    for text, x_abs, col_w, align, bold, clr in cells:
        c.setFillColor(clr if clr else BLACK)
        c.setFont('Helvetica-Bold' if bold else 'Helvetica', 7.5)
        if align == 'right':
            c.drawRightString(x_abs + col_w, y - 3, str(text))
        else:
            c.drawString(x_abs, y - 3, str(text))
    _rule(c, y - rh + 3, thickness=0.25)
    return y - rh

def _table_total_row(c, y, cells, bg, fg):
    """Colored total/summary row for tables."""
    rh = 14
    c.setFillColor(bg)
    c.rect(LM, y - rh + 3, CW, rh, fill=1, stroke=0)
    c.setFillColor(fg)
    c.setFont('Helvetica-Bold', 8)
    for text, x_abs, col_w, align in cells:
        if align == 'right':
            c.drawRightString(x_abs + col_w, y - 3, str(text))
        else:
            c.drawString(x_abs, y - 3, str(text))
    _rule(c, y - rh + 3, thickness=0.25)
    return y - rh


def _draw_footer(c, page_num, total_pages, project_name, generated_str):
    """Navy footer bar at the bottom of every page."""
    c.setFillColor(NAVY)
    c.rect(0, 0, PW, 30, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont('Helvetica', 6.5)
    c.drawString(LM, 10, f'AquaCell V14 System Summary  |  {project_name}  |  {generated_str}')
    c.setFont('Helvetica-Bold', 6.5)
    c.drawRightString(PW - RM, 10, f'Page {page_num} of {total_pages}')


def _draw_page1_header(c, logo_path, project_name, project_num, location, client, generated_str):
    """Branded navy header band + project info box. Returns y below."""
    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Underground Stormwater System')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'Preliminary System Summary — For Engineering Review Only')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 6

    # Project info box — 2-column grid
    info_h = 48
    c.setFillColor(LGRAY)
    c.setStrokeColor(MGRAY)
    c.setLineWidth(0.5)
    c.rect(LM, y - info_h, CW, info_h, fill=1, stroke=1)

    col1_x = LM + 10
    col2_x = LM + CW / 2 + 10

    def _pf(lbl, val, x, top_y):
        c.setFillColor(GRAY)
        c.setFont('Helvetica', 6)
        c.drawString(x, top_y - 2, lbl.upper())
        c.setFillColor(BLACK)
        c.setFont('Helvetica-Bold', 8.5)
        c.drawString(x, top_y - 13, val if val else '—')

    _pf('Project Name',       project_name, col1_x, y - 4)
    _pf('Project Number',     project_num,  col2_x, y - 4)
    _pf('Location',           location,     col1_x, y - 26)
    _pf('Client / Estimator', client,       col2_x, y - 26)

    return y - info_h - 8


def _draw_subsequent_header(c, logo_path, project_name, generated_str, section_title):
    """Compact header for pages 2+. Returns starting y."""
    band_h = 36
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 4, width=100, height=28,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(LM + 110, PH - 22, section_title)
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 22, f'{project_name}  |  {generated_str}')
    return PH - band_h - 12


def _draw_disclaimer_block(c, y_bottom):
    """
    Render the full disclaimer box so its BOTTOM edge sits at y_bottom.
    Returns the y of the top of the box (useful if you need to know how much space it took).
    """
    disclaimer_lines_raw = (
        "DISCLAIMER: This calculator provides preliminary, conceptual estimates only and is not a "
        "stamped engineering design. Wavin's assistance in sizing or product selection is advisory "
        "and does not constitute design responsibility or guarantee system performance. The Engineer "
        "of Record (EoR) is solely responsible for verifying all design parameters and site "
        "conditions, including hydrology, structural requirements, soils, environmental factors, and "
        "integration with the overall stormwater system. AquaCell dimensions and assumptions "
        "(including usable storage and unit base areas) follow published product data. "
        "FINAL LAYOUTS, CAPACITIES, AND INSTALLATION DEPTHS MUST BE CONFIRMED BY A LICENSED "
        "PROFESSIONAL ENGINEER using project-specific plans (grading, pipe sizes and materials, "
        "invert elevations, loading conditions, and applicable codes/standards)."
    )
    font_sz  = 6.5
    line_h   = 9
    pad      = 7
    max_w    = CW - pad * 2

    # Word-wrap
    c.setFont('Helvetica', font_sz)
    words = disclaimer_lines_raw.split()
    lines = []
    cur   = ''
    for w in words:
        test = (cur + ' ' + w).strip()
        if c.stringWidth(test, 'Helvetica', font_sz) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    box_h   = len(lines) * line_h + pad * 2
    box_top = y_bottom + box_h

    # Amber box
    c.setFillColor(LTAMB)
    c.setStrokeColor(AMBER)
    c.setLineWidth(0.6)
    c.rect(LM, y_bottom, CW, box_h, fill=1, stroke=1)

    ty = box_top - pad - font_sz + 1
    prefix = 'DISCLAIMER: '
    prefix_w = c.stringWidth(prefix, 'Helvetica-Bold', font_sz)

    for i, line in enumerate(lines):
        if i == 0:
            # Bold amber prefix
            c.setFont('Helvetica-Bold', font_sz)
            c.setFillColor(AMBER)
            c.drawString(LM + pad, ty, prefix)
            # Normal black remainder
            rest = line[len(prefix):] if line.startswith(prefix) else line
            c.setFont('Helvetica', font_sz)
            c.setFillColor(BLACK)
            c.drawString(LM + pad + prefix_w, ty, rest)
        elif 'FINAL LAYOUTS' in line or 'LICENSED PROFESSIONAL' in line or 'INSTALLATION DEPTHS' in line:
            c.setFont('Helvetica-Bold', font_sz)
            c.setFillColor(BLACK)
            c.drawString(LM + pad, ty, line)
        else:
            c.setFont('Helvetica', font_sz)
            c.setFillColor(BLACK)
            c.drawString(LM + pad, ty, line)
        ty -= line_h

    return box_top


# ══════════════════════════════════════════════════════════════════
#  DOWNLOAD PDF  — Technical Summary
# ══════════════════════════════════════════════════════════════════

@app.route('/download_pdf', methods=['POST'])
def download_pdf():

    # ── Collect form fields ──────────────────────────────────────
    project_name      = request.form.get('project_name', 'Project')
    project_num       = request.form.get('project_num', '')
    location          = request.form.get('location', '')
    client            = request.form.get('client', '')
    config            = request.form.get('config', 'SC')
    layers            = int(request.form.get('layers', 3))
    surface_elev      = float(request.form.get('surface_elev', 0) or 0)
    tank_bottom_elev  = float(request.form.get('tank_bottom_elev', 0) or 0)
    traffic_load      = request.form.get('traffic_load', 'HS20')
    known_width       = float(request.form.get('known_width', 0) or 0)
    known_length      = float(request.form.get('known_length', 0) or 0)
    perimeter_stone_width = float(request.form.get('perimeter_stone_width', 1.0) or 1.0)
    cover_stone       = float(request.form.get('cover_stone', 1.0) or 1.0)
    base_stone        = float(request.form.get('base_stone', 0.333) or 0.333)
    stone_void        = float(request.form.get('stone_void', 0.40) or 0.40)
    geoWaste          = int(request.form.get('geoWaste', 10) or 10)
    project_notes     = request.form.get('project_notes', '')
    min_storage       = float(request.form.get('min_storage', 0) or 0)
    include_stage_storage = request.form.get('include_stage_storage') == 'yes'
    include_schematic     = request.form.get('include_schematic') == 'yes'
    schematic_image       = request.form.get('schematic_image')
    stage_increment_in    = int(request.form.get('stage_increment_in', 12) or 12)
    shape_mode            = request.form.get('shape_mode', 'rectangle')

    MODULE_WID = 1.9685
    MODULE_LEN = 3.937

    if config == 'SC':
        layer_heights   = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
        void_ratio      = 0.95486
        side_multiplier = 1.312336
    else:
        layer_heights   = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
        void_ratio      = 0.92633
        side_multiplier = 1.509186351

    tank_height        = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]
    total_system_depth = base_stone + tank_height + cover_stone

    # ── Geometry ─────────────────────────────────────────────────
    if shape_mode == 'complex':
        complex_scaled_area = float(request.form.get('complex_scaled_area', 0) or 0)
        complex_tank_perim  = float(request.form.get('complex_tank_perim', 0) or 0)
        complex_known_dim   = float(request.form.get('complex_known_dim', 0) or 0)
        complex_excav_area  = float(request.form.get('complex_excav_area', 0) or 0)
        complex_excav_perim = float(request.form.get('complex_excav_perim', 0) or 0)
        other_dim    = complex_scaled_area / complex_known_dim if complex_known_dim > 0 else 0
        crates_known = math.floor(complex_known_dim / MODULE_WID) if complex_known_dim > 0 else 0
        crates_other = math.floor(other_dim / MODULE_LEN) if other_dim > 0 else 0
        snapped_known = crates_known * MODULE_WID
        snapped_other = crates_other * MODULE_LEN
        complex_tank_area = snapped_known * snapped_other
        crates_per_layer  = crates_known * crates_other
        num_crates        = crates_per_layer * layers
        gross_tank_vol    = complex_tank_area * tank_height
        tank_storage      = gross_tank_vol * void_ratio
        tank_perim_calc   = complex_tank_perim
        if complex_excav_area <= 0:
            complex_excav_area = (snapped_known + 2*perimeter_stone_width) * (snapped_other + 2*perimeter_stone_width)
        if complex_excav_perim <= 0:
            complex_excav_perim = complex_tank_perim + 8*perimeter_stone_width
        total_excavation_vol  = complex_excav_area * total_system_depth
        stone_envelope_volume = total_excavation_vol - gross_tank_vol
        total_stone_storage   = stone_envelope_volume * stone_void
        total_storage         = tank_storage + total_stone_storage
        geoTank  = round((2*complex_tank_area + complex_tank_perim*tank_height) * (1 + geoWaste/100.0), 1)
        geoStone = round((2*complex_excav_area + complex_excav_perim*total_system_depth) * (1 + geoWaste/100.0), 1)
        geoTotal = round(geoTank + geoStone, 1)
        used_perimeter = complex_tank_perim
        tank_width     = round(snapped_known, 2)
        tank_length    = round(snapped_other, 2)
    else:
        crates_wide = math.floor(known_width / MODULE_WID)
        crates_long = math.floor(known_length / MODULE_LEN)
        tank_width  = crates_wide * MODULE_WID
        tank_length = crates_long * MODULE_LEN
        num_crates     = crates_wide * crates_long * layers
        gross_tank_vol = num_crates * (MODULE_WID * MODULE_LEN * tank_height / layers)
        tank_storage   = gross_tank_vol * void_ratio
        outer_width  = tank_width  + 2*perimeter_stone_width
        outer_length = tank_length + 2*perimeter_stone_width
        total_excavation_vol  = outer_width * outer_length * total_system_depth
        stone_envelope_volume = total_excavation_vol - gross_tank_vol
        total_stone_storage   = stone_envelope_volume * stone_void
        total_storage         = tank_storage + total_stone_storage
        used_perimeter  = 2 * (tank_width + tank_length)
        tank_perim_calc = used_perimeter
        geoTank  = round((2*tank_width*tank_length + used_perimeter*tank_height) * (1 + geoWaste/100.0), 1)
        geoStone = round((outer_width*outer_length*2 + outer_width*total_system_depth*2 + outer_length*total_system_depth*2) * (1 + geoWaste/100.0), 1)
        geoTotal = round(geoTank + geoStone, 1)
        complex_tank_area = complex_tank_perim = complex_excav_area = complex_excav_perim = None
        complex_scaled_area = complex_known_dim = 0.0
        crates_known = crates_wide   # alias for display
        crates_other = crates_long   # alias for display

    stone_envelope_with_overage = stone_envelope_volume * 1.10

    if shape_mode == 'complex':
        excav_area_for_layers = complex_excav_area
        tank_footprint        = complex_tank_area
    else:
        excav_area_for_layers = outer_width * outer_length
        tank_footprint        = tank_width * tank_length

    stone_top_gross      = round(excav_area_for_layers * cover_stone, 1)
    stone_top_net        = round(stone_top_gross * stone_void, 1)
    stone_perim_gross    = round((excav_area_for_layers - tank_footprint) * tank_height, 1)
    stone_perim_net      = round(stone_perim_gross * stone_void, 1)
    stone_base_gross     = round(excav_area_for_layers * base_stone, 1)
    stone_base_net       = round(stone_base_gross * stone_void, 1)
    stone_layer_total_net = round(stone_top_net + stone_perim_net + stone_base_net, 1)

    # Stage storage
    stage_storage_lines = []
    if include_stage_storage:
        increment_ft = stage_increment_in / 12.0
        top_of_stone = tank_bottom_elev + tank_height + cover_stone
        current_elev = tank_bottom_elev - base_stone
        while current_elev <= top_of_stone + 0.01:
            depth_in_tank  = max(0, min(tank_height, current_elev - tank_bottom_elev))
            tank_vol  = (depth_in_tank / tank_height) * tank_storage if tank_height > 0 else 0
            depth_in_stone = max(0, min(total_system_depth, current_elev - (tank_bottom_elev - base_stone)))
            stone_vol = (depth_in_stone / total_system_depth) * total_stone_storage if total_system_depth > 0 else 0
            stage_storage_lines.append((
                round(current_elev, 2),
                round(tank_vol, 1),
                round(stone_vol, 1),
                round(tank_vol + stone_vol, 1)
            ))
            current_elev += increment_ft

    # Cover / load
    cover_depth = round(surface_elev - (tank_bottom_elev + tank_height), 2)
    if traffic_load == 'H10':
        min_cover_req = 1.0
    elif traffic_load == 'HS20':
        min_cover_req = 1.5 if config == 'SC' else 1.33
    elif traffic_load == 'HS25':
        min_cover_req = 2.5 if config == 'SC' else 1.83
    else:
        min_cover_req = 1.0
    max_cover_req   = 14.4 if config == 'SC' else 26.2
    cover_status    = 'PASS' if cover_depth >= min_cover_req else 'FAIL'
    max_cov_status  = 'PASS' if cover_depth <= max_cover_req else 'FAIL'
    dead_load_psi   = round(cover_stone * 120 / 144, 2)
    max_compressive = 70 if config == 'SC' else 100
    fos_dead        = round(max_compressive / dead_load_psi, 2) if dead_load_psi > 0 else None
    fos_dl_status   = 'PASS' if fos_dead and fos_dead >= 1.95 else 'FAIL'
    ll_psi, _, fos_live_load, _ = calc_live_load_fos(traffic_load, cover_depth, config)
    fos_ll_status   = 'PASS' if fos_live_load and fos_live_load >= 1.75 else 'FAIL'
    total_pressure  = round((ll_psi or 0) + dead_load_psi, 3)
    storage_status  = 'PASS' if min_storage > 0 and total_storage >= min_storage else ('FAIL' if min_storage > 0 else None)

    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %H:%M')
    logo_path     = os.path.join(app.static_folder, 'aquacell-logo.png')
    traffic_map   = {'H10': 'H-10', 'HS20': 'HS-20', 'HS25': 'HS-25'}
    tl_label      = traffic_map.get(traffic_load, traffic_load)
    stone_void_pct = int(round(stone_void * 100))

    # ── Page count ───────────────────────────────────────────────
    total_pages = 1
    if include_stage_storage and stage_storage_lines:
        rows_pp = int((PH - 110) / 13)
        total_pages += max(1, math.ceil(len(stage_storage_lines) / rows_pp))
    if include_schematic and schematic_image:
        total_pages += 1

    # ════════════════════════════════════════════════════════════
    #  BUILD PDF
    # ════════════════════════════════════════════════════════════
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)

    # ─────────────────────────────────────────────────────────
    #  PAGE 1 — Technical Summary (NO disclaimer here)
    # ─────────────────────────────────────────────────────────
    y = _draw_page1_header(c, logo_path, project_name, project_num, location, client, generated_str)

    # shared column anchors used by backfill table below
    C_LAYER   = LM + 5
    COL_NET_W = 110
    C_NET_R   = LM + CW - 5

    # ── SECTION 1: Storage Summary ────────────────────────────
    y = _section_header(c, y, '■  STORAGE SUMMARY')

    if min_storage > 0:
        y = _kv_row_colored(c, y, 'Minimum Required Storage Volume',
                            f'{min_storage:,.1f} ft³', storage_status or 'FAIL', shade=False)
    y = _kv_row(c, y, 'AquaCell Tank Storage  (crate void space)', f'{tank_storage:,.1f} ft³', shade=True)
    y = _kv_row(c, y, 'Stone Backfill Storage  (@ entered stone void ratio)', f'{total_stone_storage:,.1f} ft³', shade=False)
    y = _highlight_row(c, y, 'Total System Storage', f'{total_storage:,.1f} ft³',
                       bg=LTBLUE, fg=BLUE)
    y -= 5

    # ── SECTION 2: System Configuration ──────────────────────
    y = _section_header(c, y, '■  SYSTEM CONFIGURATION')

    config_label = f'AquaCell {config} — {"Standard" if config == "SC" else "Extra Strong (EX)"}'
    y = _kv_row(c, y, 'Configuration', config_label, shade=False)
    y = _kv_row(c, y, 'Number of Layers', str(layers), shade=True)

    if shape_mode == 'complex':
        y = _kv_row(c, y, 'Shape Mode', 'Complex Shape', shade=False)
        y = _kv_row(c, y, 'Scaled Plan Area', f'{complex_scaled_area} ft²', shade=True)
        y = _kv_row(c, y, 'Scaled Tank Perimeter', f'{complex_tank_perim} ft', shade=False)
        y = _kv_row(c, y, 'Known Dimension (from plans)', f'{complex_known_dim} ft', shade=True)
        derived = round(complex_scaled_area / complex_known_dim, 2) if complex_known_dim > 0 else 0
        y = _kv_row(c, y, 'Derived Dimension', f'{derived} ft', shade=False)
        y = _kv_row(c, y,
                    'Real-World Snapped Tank',
                    f'{tank_width} ft × {tank_length} ft  (= {round(complex_tank_area,1)} ft²)',
                    shade=True)
        y = _kv_row(c, y, 'Excavation Envelope Area', f'{round(complex_excav_area,1)} ft²', shade=False)
        y = _kv_row(c, y, 'Excavation Envelope Perimeter', f'{round(complex_excav_perim,1)} ft', shade=True)
    else:
        y = _kv_row(c, y, 'Shape Mode', 'Rectangle', shade=False)
        y = _kv_row(c, y,
                    'Snapped Tank Dimensions',
                    f'{round(tank_width,2)} ft × {round(tank_length,2)} ft × {round(tank_height,2)} ft',
                    shade=True)
        y = _kv_row(c, y, 'Tank Footprint Area', f'{round(tank_width*tank_length,1)} ft²', shade=False)
        y = _kv_row(c, y, 'Tank Perimeter', f'{round(used_perimeter,2)} ft', shade=True)

    y = _kv_row(c, y, 'Tank Height', f'{round(tank_height,3)} ft  ({round(tank_height*12,2)} in)', shade=False)
    y = _kv_row(c, y, 'Total System Depth  (base stone + tank + cover stone)',
                f'{round(total_system_depth,3)} ft  ({round(total_system_depth*12,1)} in)', shade=True)
    y = _kv_row(c, y, 'Perimeter Stone Width', f'{perimeter_stone_width} ft', shade=False)
    y = _kv_row(c, y, 'Cover Stone Depth', f'{cover_stone} ft', shade=True)
    y = _kv_row(c, y, 'Base Stone Depth', f'{base_stone} ft', shade=False)
    y -= 5

    # ── SECTION 3: Cover & Load Verification ─────────────────
    y = _section_header(c, y, '■  COVER & LOAD VERIFICATION')

    y = _sub_label(c, y, 'ELEVATIONS')
    y = _kv_row(c, y, 'Surface Elevation', f'{surface_elev} ft', shade=False)
    y = _kv_row(c, y, 'Tank Bottom Elevation', f'{tank_bottom_elev} ft', shade=True)
    y = _kv_row(c, y, 'Tank Top Elevation', f'{round(tank_bottom_elev + tank_height, 2)} ft', shade=False)

    y = _sub_label(c, y, 'COVER DEPTH')
    y = _kv_row_colored(c, y, f'Actual Cover Depth  (min req. {min_cover_req} ft)',
                        f'{cover_depth} ft', cover_status, shade=True)

    y = _sub_label(c, y, f'LIVE LOAD  ({tl_label} — ASTM F2787 2V:1H Distribution,  m=1.2, IM=24.75%, LLDF=1.15)')
    y = _kv_row(c, y, 'Factored Live Load Pressure', f'{ll_psi if ll_psi else "—"} psi', shade=True)
    y = _kv_row(c, y, 'Total Pressure  (LL + DL)', f'{total_pressure} psi', shade=False)
    y = _kv_row(c, y, f'Max Compressive Strength  ({config})', f'{max_compressive} psi', shade=True)
    y = _kv_row_colored(c, y, 'Factor of Safety — Live Load  (min recommended 1.75)',
                        str(fos_live_load if fos_live_load else '—'), fos_ll_status, shade=False)
    y -= 5

    # ── SECTION 4: Stone Backfill ─────────────────────────────
    y = _section_header(c, y, '■  STONE BACKFILL  (provided by others — coordination estimates only)')

    y = _sub_label(c, y, f'NET STORAGE BY LAYER  (stone void ratio = {stone_void_pct}%  |  net = gross × void ratio)')

    y = _table_header(c, y, [
        ('Layer',              C_LAYER, 200, 'left'),
        ('Net Storage (ft³)',  C_NET_R - COL_NET_W, COL_NET_W, 'right'),
    ])
    y = _table_row(c, y, [
        ('Cover / Top',             C_LAYER, 200, 'left',  False, None),
        (f'{stone_top_net:,.1f}',   C_NET_R - COL_NET_W, COL_NET_W, 'right', False, None),
    ], shade=False)
    y = _table_row(c, y, [
        ('Perimeter / Sides',       C_LAYER, 200, 'left',  False, None),
        (f'{stone_perim_net:,.1f}', C_NET_R - COL_NET_W, COL_NET_W, 'right', False, None),
    ], shade=True)
    y = _table_row(c, y, [
        ('Base / Bottom',           C_LAYER, 200, 'left',  False, None),
        (f'{stone_base_net:,.1f}',  C_NET_R - COL_NET_W, COL_NET_W, 'right', False, None),
    ], shade=False)
    y = _table_total_row(c, y, [
        ('TOTAL NET STORAGE',                     C_LAYER, 200, 'left'),
        (f'{stone_layer_total_net:,.1f} ft³',     C_NET_R - COL_NET_W, COL_NET_W, 'right'),
    ], bg=LTGRN, fg=GREEN)
    y -= 5

    # ── SECTION 5: Project Notes ──────────────────────────────
    if project_notes.strip():
        y = _section_header(c, y, '■  PROJECT NOTES / SPECIAL INSTRUCTIONS / ASSUMPTIONS')
        c.setFont('Helvetica', 7.5)
        c.setFillColor(BLACK)
        max_note_w = CW - 10
        note_words = project_notes.split()
        note_line  = ''
        for word in note_words:
            test = (note_line + ' ' + word).strip()
            if c.stringWidth(test, 'Helvetica', 7.5) <= max_note_w:
                note_line = test
            else:
                if y < 80:
                    break
                c.drawString(LM + 5, y - 3, note_line)
                y -= 12
                note_line = word
        if note_line and y >= 80:
            c.drawString(LM + 5, y - 3, note_line)
            y -= 12

    _draw_footer(c, 1, total_pages, project_name, generated_str)

    # ─────────────────────────────────────────────────────────
    #  STAGE STORAGE PAGES
    # ─────────────────────────────────────────────────────────
    page_num = 1
    if include_stage_storage and stage_storage_lines:
        page_num += 1
        c.showPage()
        y = _draw_subsequent_header(c, logo_path, project_name, generated_str,
                                    f'Stage Storage Table — {stage_increment_in}-Inch Increments')
        y -= 6

        # Stage storage table — 4 columns, all spanning full CW
        # Pin right edges relative to LM + CW
        SS_RE   = LM + CW - 5           # rightmost edge
        SS_W    = 100                    # width of each numeric column
        SS_GAP  = 8                      # gap between columns

        # Column right edges (right to left)
        C4_R = SS_RE                          # Total Storage
        C3_R = C4_R - SS_W - SS_GAP          # Stone Storage
        C2_R = C3_R - SS_W - SS_GAP          # Tank Storage
        # Elevation label is left-aligned from LM+5

        y = _table_header(c, y, [
            ('Elevation (ft)',      C_LAYER, 120, 'left'),
            ('Tank Storage (ft³)',  C2_R - SS_W, SS_W, 'right'),
            ('Stone Storage (ft³)', C3_R - SS_W, SS_W, 'right'),
            ('Total Storage (ft³)', C4_R - SS_W, SS_W, 'right'),
        ])

        for i, (elev, tank_v, stone_v, total_v) in enumerate(stage_storage_lines):
            if y < 55:
                _draw_footer(c, page_num, total_pages, project_name, generated_str)
                page_num += 1
                c.showPage()
                y = _draw_subsequent_header(c, logo_path, project_name, generated_str,
                                            f'Stage Storage Table — {stage_increment_in}-Inch Increments (continued)')
                y -= 6
                y = _table_header(c, y, [
                    ('Elevation (ft)',      C_LAYER, 120, 'left'),
                    ('Tank Storage (ft³)',  C2_R - SS_W, SS_W, 'right'),
                    ('Stone Storage (ft³)', C3_R - SS_W, SS_W, 'right'),
                    ('Total Storage (ft³)', C4_R - SS_W, SS_W, 'right'),
                ])
            y = _table_row(c, y, [
                (f'{elev:.2f}',    C_LAYER,    120,  'left',  False, None),
                (f'{tank_v:.1f}',  C2_R - SS_W, SS_W, 'right', False, None),
                (f'{stone_v:.1f}', C3_R - SS_W, SS_W, 'right', False, None),
                (f'{total_v:.1f}', C4_R - SS_W, SS_W, 'right', True,  BLUE),
            ], shade=(i % 2 == 1))

        _draw_footer(c, page_num, total_pages, project_name, generated_str)

    # ─────────────────────────────────────────────────────────
    #  SCHEMATIC PAGE  (disclaimer lives at the bottom here)
    # ─────────────────────────────────────────────────────────
    if include_schematic and schematic_image:
        page_num += 1
        c.showPage()
        y = _draw_subsequent_header(c, logo_path, project_name, generated_str,
                                    'Conceptual Plan View / Section Schematic')
        y -= 10

        # Disclaimer anchored just above the footer (footer = 30 pts tall)
        footer_top  = 30
        disc_gap    = 6       # gap between disclaimer box and footer
        # measure disclaimer height first (word-wrap at same settings)
        font_sz   = 6.5
        line_h    = 9
        pad       = 7
        max_disc_w = CW - pad * 2
        disc_text = (
            "DISCLAIMER: This calculator provides preliminary, conceptual estimates only and is not a "
            "stamped engineering design. Wavin's assistance in sizing or product selection is advisory "
            "and does not constitute design responsibility or guarantee system performance. The Engineer "
            "of Record (EoR) is solely responsible for verifying all design parameters and site "
            "conditions, including hydrology, structural requirements, soils, environmental factors, and "
            "integration with the overall stormwater system. AquaCell dimensions and assumptions "
            "(including usable storage and unit base areas) follow published product data. "
            "FINAL LAYOUTS, CAPACITIES, AND INSTALLATION DEPTHS MUST BE CONFIRMED BY A LICENSED "
            "PROFESSIONAL ENGINEER using project-specific plans (grading, pipe sizes and materials, "
            "invert elevations, loading conditions, and applicable codes/standards)."
        )
        c.setFont('Helvetica', font_sz)
        d_words = disc_text.split()
        d_lines = []
        cur = ''
        for w in d_words:
            test = (cur + ' ' + w).strip()
            if c.stringWidth(test, 'Helvetica', font_sz) <= max_disc_w:
                cur = test
            else:
                if cur:
                    d_lines.append(cur)
                cur = w
        if cur:
            d_lines.append(cur)
        disc_box_h = len(d_lines) * line_h + pad * 2
        disc_bottom = footer_top + disc_gap
        disc_top    = disc_bottom + disc_box_h

        # Image fills space above disclaimer
        avail_h = y - disc_top - 6
        if avail_h > 60:
            try:
                image_data = base64.b64decode(schematic_image.split(',')[1])
                img = ImageReader(BytesIO(image_data))
                c.drawImage(img, LM, disc_top + 6, width=CW, height=avail_h,
                            preserveAspectRatio=True, mask='auto')
            except Exception:
                c.setFont('Helvetica', 9)
                c.setFillColor(GRAY)
                c.drawString(LM, y - 30, 'Schematic image could not be rendered.')

        # Draw disclaimer at bottom
        _draw_disclaimer_block(c, disc_bottom)

        _draw_footer(c, page_num, total_pages, project_name, generated_str)

    c.save()
    buffer.seek(0)
    safe_name = (project_name or 'Project').strip().replace(' ', '_')
    return send_file(buffer, as_attachment=True,
                     download_name=f'AquaCell_Summary_{safe_name}.pdf',
                     mimetype='application/pdf')




# ══════════════════════════════════════════════════════════════════
#  DOWNLOAD QUOTE  — Materials Quote PDF
# ══════════════════════════════════════════════════════════════════

@app.route('/download_quote', methods=['POST'])
def download_quote():
  try:

      # ── Collect form fields ──────────────────────────────────────
      project_name     = request.form.get('project_name', '')
      project_num      = request.form.get('project_num', '')
      location         = request.form.get('location', '')
      client           = request.form.get('client', '')
      config           = request.form.get('config', 'SC')
      layers           = int(request.form.get('layers', 3))
      traffic_load     = request.form.get('traffic_load', 'HS20')
      known_width      = float(request.form.get('known_width', 0) or 0)
      known_length     = float(request.form.get('known_length', 0) or 0)
      perimeter_stone_width = float(request.form.get('perimeter_stone_width', 1.0) or 1.0)
      cover_stone      = float(request.form.get('cover_stone', 1.0) or 1.0)
      base_stone       = float(request.form.get('base_stone', 0.333) or 0.333)
      stone_void       = float(request.form.get('stone_void', 0.40) or 0.40)
      geoWaste         = int(request.form.get('geoWaste', 10) or 10)
      min_storage      = float(request.form.get('min_storage', 0) or 0)
      pipe_connectors  = int(request.form.get('pipe_connectors', 0) or 0)
      top_adapters_12  = int(request.form.get('top_adapters_12', 0) or 0)
      top_adapters_16  = int(request.form.get('top_adapters_16', 0) or 0)
      project_notes    = request.form.get('project_notes', '')
      shape_mode       = request.form.get('shape_mode', 'rectangle')

      # Pricing
      subtotal         = float(request.form.get('totalAquaCellCost', 0) or 0)
      freight_cost     = float(request.form.get('freightCost', 0) or 0)
      total_with_freight = float(request.form.get('totalWithFreight', 0) or subtotal)
      freight_pct      = float(request.form.get('freightPct', 10) or 10)
      cost_per_ft3     = float(request.form.get('costPerFt3Hidden', 0) or 0)

      MODULE_WID = 1.9685
      MODULE_LEN = 3.937

      if config == 'SC':
          layer_heights   = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
          void_ratio      = 0.95486
          side_multiplier = 1.312336
      else:
          layer_heights   = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
          void_ratio      = 0.92633
          side_multiplier = 1.509186351

      tank_height        = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]
      total_system_depth = base_stone + tank_height + cover_stone

      # Geometry
      if shape_mode == 'complex':
          complex_scaled_area = float(request.form.get('complex_scaled_area', 0) or 0)
          complex_tank_perim  = float(request.form.get('complex_tank_perim', 0) or 0)
          complex_known_dim   = float(request.form.get('complex_known_dim', 0) or 0)
          complex_excav_area  = float(request.form.get('complex_excav_area', 0) or 0)
          complex_excav_perim = float(request.form.get('complex_excav_perim', 0) or 0)
          other_dim    = complex_scaled_area / complex_known_dim if complex_known_dim > 0 else 0
          crates_known = math.floor(complex_known_dim / MODULE_WID) if complex_known_dim > 0 else 0
          crates_other = math.floor(other_dim / MODULE_LEN) if other_dim > 0 else 0
          snapped_known = crates_known * MODULE_WID
          snapped_other = crates_other * MODULE_LEN
          complex_tank_area = snapped_known * snapped_other
          crates_per_layer  = crates_known * crates_other
          num_crates        = crates_per_layer * layers
          gross_tank_vol    = complex_tank_area * tank_height
          tank_storage      = gross_tank_vol * void_ratio
          tank_perim_calc   = complex_tank_perim
          side_plates       = round(tank_perim_calc * (layers * side_multiplier) / 5.17)
          if config == 'SC':
              base_units    = num_crates
              bottom_plates = crates_per_layer
          else:
              base_units    = num_crates * 2
              bottom_plates = 0
          if complex_excav_area <= 0:
              complex_excav_area = (snapped_known + 2*perimeter_stone_width) * (snapped_other + 2*perimeter_stone_width)
          if complex_excav_perim <= 0:
              complex_excav_perim = complex_tank_perim + 8*perimeter_stone_width
          total_excavation_vol  = complex_excav_area * total_system_depth
          stone_envelope_volume = total_excavation_vol - gross_tank_vol
          total_stone_storage   = stone_envelope_volume * stone_void
          total_storage         = tank_storage + total_stone_storage
          geoTank  = round((2*complex_tank_area + complex_tank_perim*tank_height) * (1 + geoWaste/100.0), 1)
          geoStone = round((2*complex_excav_area + complex_excav_perim*total_system_depth) * (1 + geoWaste/100.0), 1)
          used_perimeter = complex_tank_perim
          tank_width     = round(snapped_known, 2)
          tank_length    = round(snapped_other, 2)
          tank_area      = round(complex_tank_area, 1)
      else:
          crates_wide = math.floor(known_width / MODULE_WID)
          crates_long = math.floor(known_length / MODULE_LEN)
          tank_width  = crates_wide * MODULE_WID
          tank_length = crates_long * MODULE_LEN
          tank_area   = round(tank_width * tank_length, 1)
          num_crates  = crates_wide * crates_long * layers
          gross_tank_vol = num_crates * (MODULE_WID * MODULE_LEN * tank_height / layers)
          tank_storage   = gross_tank_vol * void_ratio
          outer_width  = tank_width  + 2*perimeter_stone_width
          outer_length = tank_length + 2*perimeter_stone_width
          total_excavation_vol  = outer_width * outer_length * total_system_depth
          stone_envelope_volume = total_excavation_vol - gross_tank_vol
          total_stone_storage   = stone_envelope_volume * stone_void
          total_storage         = tank_storage + total_stone_storage
          used_perimeter  = 2 * (tank_width + tank_length)
          tank_perim_calc = used_perimeter
          side_plates     = round(tank_perim_calc * (layers * side_multiplier) / 5.17)
          if config == 'SC':
              base_units    = num_crates
              bottom_plates = crates_wide * crates_long
          else:
              base_units    = num_crates * 2
              bottom_plates = 0
          geoTank  = round((2*tank_width*tank_length + used_perimeter*tank_height) * (1 + geoWaste/100.0), 1)
          geoStone = round((outer_width*outer_length*2 + outer_width*total_system_depth*2 + outer_length*total_system_depth*2) * (1 + geoWaste/100.0), 1)

      geoTank_yd2  = round(geoTank  / 9, 0)
      geoStone_yd2 = round(geoStone / 9, 0)
      stone_yd3    = round(stone_envelope_volume * 1.10 / 27, 0)

      config_label = f'{config}-{layers}'   # e.g. SC-5
      generated_str = datetime.datetime.now().strftime('%m/%d/%Y')
      logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')

      def money(v):
          return f'${v:,.2f}'

      # ── Build PDF using canvas (matching template layout) ────────
      buffer = io.BytesIO()
      c = canvas.Canvas(buffer, pagesize=letter)
      W, H = letter   # 612 x 792

      # ── Helpers local to quote ────────────────────────────────────
      QNY  = colors.HexColor('#003366')   # dark navy
      QRED = colors.HexColor('#c0392b')
      QGRN = colors.HexColor('#1a7a3a')
      QYLW = colors.HexColor('#f7dc6f')
      QLGY = colors.HexColor('#f2f2f2')
      QMGY = colors.HexColor('#d0d0d0')
      QTBL = colors.HexColor('#2980b9')

      LQ = 28   # left margin for quote
      RQ = 28   # right margin
      QW = W - LQ - RQ  # 556

      def q_rule(cy, thick=0.5, clr=QMGY):
          c.setStrokeColor(clr)
          c.setLineWidth(thick)
          c.line(LQ, cy, LQ + QW, cy)

      def q_rect(x, y, w, h, fill, stroke=None, stroke_clr=None, radius=0):
          c.setFillColor(fill)
          if stroke is not None and stroke_clr:
              c.setStrokeColor(stroke_clr)
              c.setLineWidth(stroke)
          if radius:
              c.roundRect(x, y, w, h, radius, fill=1, stroke=1 if stroke else 0)
          else:
              c.rect(x, y, w, h, fill=1, stroke=1 if stroke else 0)

      def q_text(x, y, text, font='Helvetica', size=8, color=BLACK, align='left'):
          c.setFont(font, size)
          c.setFillColor(color)
          if align == 'right':
              c.drawRightString(x, y, str(text))
          elif align == 'center':
              c.drawCentredString(x, y, str(text))
          else:
              c.drawString(x, y, str(text))

      # ════════════════════════════════════════
      #  HEADER BLOCK
      # ════════════════════════════════════════
      y = H - 28

      # Logo box (top-left)
      if logo_path and os.path.exists(logo_path):
          try:
              img = ImageReader(logo_path)
              c.drawImage(img, LQ, y - 52, width=130, height=52,
                          preserveAspectRatio=True, mask='auto')
          except Exception:
              pass

      # "MATERIALS QUOTE" title (center)
      q_text(W/2, y - 14, 'MATERIALS QUOTE', 'Helvetica-Bold', 20, QNY, 'center')

      # Orbia logo area / quote number (top-right)
      q_rect(W - RQ - 140, y - 52, 140, 52, QLGY)
      q_text(W - RQ - 70, y - 18, 'An Orbia business.', 'Helvetica', 7, GRAY, 'center')
      q_text(W - RQ - 70, y - 36, project_num if project_num else '—', 'Helvetica-Bold', 18, QNY, 'center')

      y -= 62

      # ── Project info grid ────────────────────────────────────────
      q_rule(y)
      y -= 4

      # Row 1
      q_text(LQ, y - 8, 'CLIENT:', 'Helvetica-Bold', 8, QNY)
      q_text(LQ + 48, y - 8, client or '—', 'Helvetica', 8, BLACK)
      q_text(W/2, y - 8, 'DATE', 'Helvetica-Bold', 8, QNY)
      q_text(W/2 + 36, y - 8, generated_str, 'Helvetica', 8, BLACK)

      # Row 2
      q_text(LQ, y - 20, 'PROJECT NAME:', 'Helvetica-Bold', 8, QNY)
      q_text(LQ + 78, y - 20, project_name or '—', 'Helvetica', 8, BLACK)
      q_text(W/2, y - 20, 'ATTN:', 'Helvetica-Bold', 8, QNY)

      # Row 3
      q_text(LQ, y - 32, 'CITY:', 'Helvetica-Bold', 8, QNY)
      city_str = location.split(',')[0].strip() if location else '—'
      q_text(LQ + 48, y - 32, city_str, 'Helvetica', 8, BLACK)
      q_text(W/2, y - 32, 'PREPARED BY:', 'Helvetica-Bold', 8, QNY)

      # Row 4
      q_text(LQ, y - 44, 'STATE:', 'Helvetica-Bold', 8, QNY)
      state_str = location.split(',')[1].strip() if location and ',' in location else '—'
      q_text(LQ + 48, y - 44, state_str, 'Helvetica', 8, BLACK)
      q_text(W/2, y - 44, 'EMAIL:', 'Helvetica-Bold', 8, QNY)

      y -= 58
      q_rule(y)
      y -= 4

      # ── Warning banner if notes present ─────────────────────────
      if project_notes.strip():
          q_rect(LQ, y - 14, QW, 14, QYLW)
          q_text(W/2, y - 10, project_notes[:120], 'Helvetica-Bold', 7, QRED, 'center')
          y -= 18

      # ── Spec summary row ─────────────────────────────────────────
      q_rect(LQ, y - 28, QW, 28, QLGY)
      col_w = QW / 6

      specs = [
          ('AREA', f'{tank_area:,.0f} ft²'),
          ('PERIMETER', f'{round(used_perimeter,1)} ft'),
          ('Plans Dated:', generated_str),
          ('TANK HEIGHT', f'{round(tank_height,1)} ft'),
          ('MODEL TYPE', config_label),
          ('', ''),
      ]
      for i, (lbl, val) in enumerate(specs):
          x = LQ + i * col_w
          q_text(x + 4, y - 10, lbl, 'Helvetica-Bold', 7, GRAY)
          q_text(x + 4, y - 22, val, 'Helvetica-Bold', 8.5, QNY)

      y -= 32

      # Storage required + tank volume highlight
      q_rect(LQ, y - 28, QW/2, 28, QNY)
      q_text(LQ + 6, y - 10, 'TOTAL PROJECT STORAGE REQUIRED', 'Helvetica-Bold', 7, WHITE)
      req_str = f'{min_storage:,.0f} FT\u00b3' if min_storage > 0 else 'NOT SPECIFIED'
      q_text(LQ + 6, y - 22, req_str, 'Helvetica-Bold', 11, QYLW)

      q_rect(LQ + QW/2, y - 28, QW/2, 28, QTBL)
      q_text(LQ + QW/2 + 6, y - 10, 'AQUACELL TANK VOLUME  (EXCLUDING STONE)', 'Helvetica-Bold', 7, WHITE)
      q_text(LQ + QW/2 + 6, y - 22, f'{tank_storage:,.0f} FT\u00b3', 'Helvetica-Bold', 11, WHITE)

      y -= 34

      # ════════════════════════════════════════
      #  SECTION 1: AQUACELL COMPONENTS
      # ════════════════════════════════════════
      q_rect(LQ, y - 14, QW, 14, QNY)
      q_text(W/2, y - 10, 'AQUACELL STORMWATER MANAGEMENT SYSTEM — COMPONENTS', 'Helvetica-Bold', 8.5, WHITE, 'center')
      y -= 14

      # Table header
      q_rect(LQ, y - 13, QW, 13, colors.HexColor('#1a5276'))
      col_ln  = LQ + 4
      col_pc  = LQ + 32
      col_ds  = LQ + 110
      col_qt  = LQ + QW - 140
      col_un  = LQ + QW - 80
      col_nt  = LQ + QW - 30

      for lbl, x in [('LINE', col_ln), ('PART CODES', col_pc), ('DESCRIPTION', col_ds),
                     ('QUANTITY', col_qt), ('UNITS', col_un), ('NOTES', col_nt)]:
          q_text(x, y - 9, lbl, 'Helvetica-Bold', 7, WHITE)
      y -= 13

      bom_rows = [
          ('1', '3091506',    'AQUACELL BASE UNIT',               base_units,      'EACH', ''),
          ('2', '2476600003', 'AQUACELL SIDE PLATE',               side_plates,     'EACH', ''),
          ('3', '2476600001', 'AQUACELL BOTTOM PLATE',             bottom_plates,   'EACH', ''),
          ('4', '2476631200', 'AQUACELL 8\u201312\u2033 PIPE CONNECTOR', pipe_connectors, 'EACH', ''),
          ('5', '3085857',    'AQUACELL TOP CONNECTOR (12\u2033)', top_adapters_12, 'EACH', ''),
          ('5', '2476842000', 'AQUACELL TOP CONNECTOR (16\u2033)', top_adapters_16, 'EACH', ''),
          ('6', '3091506',    '**AQUACELL BASE UNITS — CONTINGENCY**', 0,          'EACH', ''),
      ]

      for i, (ln, pc, ds, qt, un, nt) in enumerate(bom_rows):
          shade = QLGY if i % 2 == 0 else WHITE
          q_rect(LQ, y - 13, QW, 13, shade)
          q_text(col_ln, y - 9, ln,       'Helvetica', 7.5, BLACK)
          q_text(col_pc, y - 9, pc,       'Helvetica', 7.5, BLACK)
          q_text(col_ds, y - 9, ds,       'Helvetica', 7.5, BLACK)
          q_text(col_qt + 30, y - 9, str(qt) if qt else '0', 'Helvetica-Bold', 7.5, QNY, 'right')
          q_text(col_un, y - 9, un,       'Helvetica', 7.5, BLACK)
          q_text(col_nt, y - 9, nt,       'Helvetica', 6.5, GRAY)
          y -= 13

      # Pricing: cost per ft³ note + subtotal
      y -= 4
      q_rect(LQ, y - 13, QW - 120, 13, QLGY)
      q_text(LQ + 4, y - 9,
             f'Pricing based on ${cost_per_ft3:.4f}/ft\u00b3 \u00d7 {tank_storage:,.1f} ft\u00b3 net AquaCell storage',
             'Helvetica-Oblique', 7, GRAY)

      q_rect(LQ + QW - 120, y - 13, 120, 13, QNY)
      q_text(LQ + QW - 4, y - 9, 'SUB-TOTAL', 'Helvetica-Bold', 7.5, WHITE, 'right')
      y -= 13
      q_rect(LQ + QW - 120, y - 13, 120, 13, QLGY)
      q_text(LQ + QW - 4, y - 9, money(subtotal), 'Helvetica-Bold', 8.5, QNY, 'right')
      y -= 18

      # ════════════════════════════════════════
      #  SECTION 2: PROVIDED BY OTHERS
      # ════════════════════════════════════════
      q_rect(LQ, y - 13, QW, 13, QRED)
      q_text(W/2, y - 9,
             'RECOMMENDED BY WAVIN — PROVIDED BY OTHERS  (ESTIMATES FOR REFERENCE ONLY — SUBJECT TO VERIFICATION)',
             'Helvetica-Bold', 7, WHITE, 'center')
      y -= 13

      # Others table header — no part codes, no notes, letter line labels
      q_rect(LQ, y - 13, QW, 13, colors.HexColor('#922b21'))
      for lbl, x in [('LINE', col_ln), ('DESCRIPTION', col_pc),
                     ('QUANTITY', col_qt), ('UNITS', col_un)]:
          q_text(x, y - 9, lbl, 'Helvetica-Bold', 7, WHITE)
      y -= 13

      alpha = ['A','B','C','D','E','F','G']
      others_rows = [
          (f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste}% WASTE (TANK ONLY)',
           int(geoTank_yd2), 'SQ YD'),
          (f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste}% WASTE (BACKFILL ONLY)',
           int(geoStone_yd2), 'SQ YD'),
          ('WOVEN GEOTEXTILE + 20% WASTE (TANK ONLY)', 0, 'SQ YD'),
          ('BIAXIAL GEOGRID (INTEGRALLY FORMED POLYPROPYLENE) + 20% WASTE', 0, 'SQ YD'),
          ('CASTINGS FOR VENTING / INSPECTION PORTS / INLETS',
           top_adapters_12 + top_adapters_16, 'EACH'),
          ('LARGE CUSTOM PIPE ADAPTERS (18\u2033\u201336\u2033) FOR FIELD INSTALL', 0, 'EACH'),
          ('STONE BACKFILL OR SELECT BACKFILL ESTIMATED FOR UG SYSTEM',
           int(stone_yd3), 'CU YD'),
      ]

      for i, (ds, qt, un) in enumerate(others_rows):
          shade = QLGY if i % 2 == 0 else WHITE
          rh = 20
          q_rect(LQ, y - rh, QW, rh, shade)
          q_text(col_ln, y - 10, alpha[i], 'Helvetica-Bold', 7.5, QNY)
          max_chars = 80
          if len(ds) <= max_chars:
              ds_line1, ds_line2 = ds, ''
          else:
              split_at = ds.rfind(' ', 0, max_chars)
              if split_at == -1:
                  split_at = max_chars
              ds_line1 = ds[:split_at]
              ds_line2 = ds[split_at+1:split_at+1+max_chars]
          q_text(col_pc, y - 8,  ds_line1, 'Helvetica', 7.5, BLACK)
          if ds_line2:
              q_text(col_pc, y - 16, ds_line2, 'Helvetica', 7.5, BLACK)
          q_text(col_qt + 30, y - 10, str(qt), 'Helvetica-Bold', 7.5, QNY, 'right')
          q_text(col_un, y - 10, un, 'Helvetica', 7.5, BLACK)
          y -= rh

      y -= 6

      # ════════════════════════════════════════
      #  TOTALS BLOCK
      # ════════════════════════════════════════
      totals = [
          ('AQUACELL SUB-TOTAL',  money(subtotal),           QNY,   QLGY),
          ('ESTIMATED TAXES*',    '$0.00  (TBD at purchase)', GRAY,  WHITE),
          (f'ESTIMATED FREIGHT* ({freight_pct:.1f}%)', money(freight_cost), colors.HexColor('#784212'), colors.HexColor('#fef9e7')),
          ('ESTIMATED TOTAL',     money(total_with_freight),  QGRN,  colors.HexColor('#eafaf1')),
      ]

      tot_label_w = 160
      tot_val_w   = 110
      tot_x_label = LQ + QW - tot_label_w - tot_val_w - 4
      tot_x_val   = LQ + QW - tot_val_w

      for lbl, val, fg, bg in totals:
          rh = 15
          q_rect(tot_x_label - 4, y - rh, tot_label_w + tot_val_w + 8, rh, bg)
          q_text(tot_x_label + tot_label_w - 4, y - 5, lbl, 'Helvetica-Bold', 8, fg, 'right')
          q_text(tot_x_val + tot_val_w - 4,     y - 5, val, 'Helvetica-Bold', 9, fg, 'right')
          q_rule(y - rh, thick=0.3, clr=QMGY)
          y -= rh

      y -= 8

      # ════════════════════════════════════════
      #  FOOTNOTES
      # ════════════════════════════════════════
      notes = [
          '*ESTIMATED TAXES & FREIGHT TO BE DETERMINED AT TIME OF PURCHASE',
          '*THIS QUOTE IS VALID FOR 30 DAYS FROM THE DATE OF ISSUANCE. SUBJECT TO CHANGE AFTER THIS DATE',
      ]
      for n in notes:
          q_text(W/2, y, n, 'Helvetica-Bold', 7, QRED, 'center')
          y -= 11

      y -= 6

      # ════════════════════════════════════════
      #  DISCLAIMER
      # ════════════════════════════════════════
      disc = (
          "Disclaimer: It is the responsibility of the Client that the technical advice provided by Wavin is "
          "reviewed and approved by a duly authorized hydraulic designer in your country. All recommendations "
          "and suggestions on the use of Wavin Products & additional accessories or materials are made without "
          "guarantee, express or implied or claim to accuracy as the conditions of use are beyond Wavin's "
          "control. The Client remains solely responsible that each product is fit for its intended purpose "
          "and the actual conditions for use are suitable. Wavin reserves the right to change products and "
          "specifications of its products without notice. Any Purchase Order between Wavin and Customer in "
          "respect of this quotation will be subject to Wavin's Terms and Conditions of Sale."
      )
      c.setFillColor(GRAY)
      c.setFont('Helvetica', 6.5)
      # Simple word-wrap at ~110 chars per line (safe, no canvas stringWidth)
      disc_words = disc.split()
      dlines = []
      dline  = ''
      for w in disc_words:
          test = (dline + ' ' + w).strip()
          if len(test) <= 118:
              dline = test
          else:
              if dline:
                  dlines.append(dline)
              dline = w
      if dline:
          dlines.append(dline)
      for dl in dlines:
          c.drawString(LQ, y, dl)
          y -= 9

      c.save()
      buffer.seek(0)
      safe_name = (project_name or 'Quote').strip().replace(' ', '_')
      return send_file(buffer, as_attachment=True,
                       download_name=f'AquaCell_Quote_{safe_name}.pdf',
                       mimetype='application/pdf')

  except Exception as e:
      import traceback
      tb = traceback.format_exc()
      return f'<pre style="color:red;padding:20px">{tb}</pre>', 500


if __name__ == '__main__':
    app.run(debug=True)
