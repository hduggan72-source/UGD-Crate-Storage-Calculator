"""
app_multi.py  —  Multi-Tank AquaCell Calculator
Branch: Multi_Tank_Calculator

HOW TO INTEGRATE:
  Option A (recommended):  Copy the three routes below into your existing app.py.
                           Add the import block at the top if any are missing.
  Option B:  Run as a separate Flask app on a different port for local testing.

All existing single-tank routes in app.py are UNTOUCHED.
"""

from flask import Flask, render_template, request, send_file, jsonify
import io, math, datetime, os, json
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader

# ── If merging into existing app.py, remove the two lines below ──
app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════
#  SHARED CONSTANTS  (identical to single-tank app.py)
# ══════════════════════════════════════════════════════════════════
MODULE_WID = 1.9685   # ft
MODULE_LEN = 3.937    # ft

CONFIG_DATA = {
    'SC': {
        'layer_heights':   [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581],
        'void_ratio':      0.95486,
        'side_multiplier': 1.312336,
        'max_strength':    70,   # psi
        'min_cover':       {'H10': 1.0, 'HS20': 1.5,  'HS25': 2.5},
        'max_cover':       14.4,
    },
    'EX': {
        'layer_heights':   [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073],
        'void_ratio':      0.92633,
        'side_multiplier': 1.509186351,
        'max_strength':    100,
        'min_cover':       {'H10': 1.0, 'HS20': 1.33, 'HS25': 1.83},
        'max_cover':       26.2,
    },
}

WEIGHTS = {
    'base': 25.180, 'side': 4.910, 'bottom': 7.860,
    'pipe': 2.703,  'adapter12': 11.023, 'adapter16': 22.000,
}
PALLETS = {
    'base': 56, 'side': 120, 'bottom': 120,
    'pipe': 60, 'adapter12': 20, 'adapter16': 12,
}

# ASTM F2787 live load constants
_LL_m    = 1.2
_LL_IM   = 0.2475
_LL_LLDF = 1.15

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
QGRN   = colors.HexColor('#1a7a3a')
QRED   = colors.HexColor('#c0392b')


# ══════════════════════════════════════════════════════════════════
#  CALC ENGINE  —  single-tank calculation (returns dict)
# ══════════════════════════════════════════════════════════════════
def calc_tank(t):
    """
    t: dict with keys matching the multi-tank form field names.
    Returns a results dict with all derived quantities.
    """
    config  = t.get('config', 'SC')
    layers  = int(t.get('layers', 3))
    cd      = CONFIG_DATA[config]

    surface_elev     = float(t.get('surface_elev', 0) or 0)
    tank_bottom_elev = float(t.get('tank_bottom_elev', 0) or 0)
    traffic_load     = t.get('traffic_load', 'HS20')
    known_width      = float(t.get('known_width', 0) or 0)
    known_length     = float(t.get('known_length', 0) or 0)
    perimeter_stone_width = float(t.get('perimeter_stone_width', 1.0) or 1.0)
    cover_stone      = float(t.get('cover_stone', 1.0) or 1.0)
    base_stone       = float(t.get('base_stone', 0.333) or 0.333)
    stone_void       = float(t.get('stone_void', 0.40) or 0.40)
    geoWaste         = int(t.get('geoWaste', 10) or 10)
    pipe_connectors  = int(t.get('pipe_connectors', 0) or 0)
    top_adapters_12  = int(t.get('top_adapters_12', 0) or 0)
    top_adapters_16  = int(t.get('top_adapters_16', 0) or 0)
    min_storage      = float(t.get('min_storage', 0) or 0)
    tank_label       = t.get('tank_label', 'Tank')

    layer_heights   = cd['layer_heights']
    void_ratio      = cd['void_ratio']
    side_multiplier = cd['side_multiplier']

    tank_height        = layer_heights[layers - 1] if layers <= len(layer_heights) else layer_heights[-1]
    total_system_depth = base_stone + tank_height + cover_stone

    # Rectangle geometry
    crates_wide    = math.floor(known_width / MODULE_WID)
    crates_long    = math.floor(known_length / MODULE_LEN)
    tank_width     = crates_wide * MODULE_WID
    tank_length    = crates_long * MODULE_LEN
    crates_layer   = crates_wide * crates_long
    num_crates     = crates_layer * layers
    gross_tank_vol = tank_width * tank_length * tank_height
    tank_storage   = gross_tank_vol * void_ratio

    outer_width    = tank_width + 2 * perimeter_stone_width
    outer_length   = tank_length + 2 * perimeter_stone_width
    total_excav_vol   = outer_width * outer_length * total_system_depth
    stone_env_vol     = total_excav_vol - gross_tank_vol
    total_stone_stor  = stone_env_vol * stone_void
    total_storage     = tank_storage + total_stone_stor

    used_perimeter = 2 * (tank_width + tank_length)
    tank_perim     = used_perimeter
    side_plates    = round(tank_perim * (layers * side_multiplier) / 5.17)

    if config == 'SC':
        base_units    = num_crates
        bottom_plates = crates_layer
    else:
        base_units    = num_crates * 2
        bottom_plates = 0

    # Contingency: round up base_units to next full pallet
    contingency = max(0, math.ceil(base_units / PALLETS['base']) * PALLETS['base'] - base_units)

    # Geotextile
    tank_top_bottom = 2 * tank_width * tank_length
    tank_sides      = used_perimeter * tank_height
    excav_area      = outer_width * outer_length
    geoTank  = round((tank_top_bottom + tank_sides) * (1 + geoWaste / 100.0), 1)
    geoStone = round((excav_area * 2 + outer_width * total_system_depth * 2 + outer_length * total_system_depth * 2) * (1 + geoWaste / 100.0), 1)
    geoTotal = round(geoTank + geoStone, 1)

    # Stone backfill bulk
    stone_yd3  = round(stone_env_vol * 1.10 / 27, 1)
    stone_tons = round(stone_env_vol * 1.10 * 100 / 2000, 2)

    # Stone by layer
    stone_top_gross   = round(excav_area * cover_stone, 1)
    stone_top_net     = round(stone_top_gross * stone_void, 1)
    stone_perim_gross = round((excav_area - tank_width * tank_length) * tank_height, 1)
    stone_perim_net   = round(stone_perim_gross * stone_void, 1)
    stone_base_gross  = round(excav_area * base_stone, 1)
    stone_base_net    = round(stone_base_gross * stone_void, 1)

    # Cover / load
    cover_depth = round(surface_elev - (tank_bottom_elev + tank_height), 2)
    min_cover   = cd['min_cover'].get(traffic_load, 1.0)
    max_cover   = cd['max_cover']
    cover_ok    = cover_depth >= min_cover
    max_cover_ok = cover_depth <= max_cover

    dead_load_psi   = round(cover_stone * 120 / 144, 2)
    max_str         = cd['max_strength']
    fos_dead        = round(max_str / dead_load_psi, 2) if dead_load_psi > 0 else None

    # ASTM F2787 live load
    wl_map = {'H10': (8000, 10, 10), 'HS20': (16000, 10, 20), 'HS25': (20000, 10, 20)}
    wl, tire_L, tire_W = wl_map.get(traffic_load, (16000, 10, 20))
    cover_in = cover_depth * 12 if cover_depth > 0 else 0
    proj_area = (tire_L + cover_in * _LL_LLDF) * (tire_W + cover_in * _LL_LLDF)
    ll_lbs = wl * _LL_m * (1 + _LL_IM) + wl / 180.0
    ll_psi = round(ll_lbs / proj_area, 3) if proj_area > 0 and cover_depth > 0 else None
    total_press = round((ll_psi or 0) + dead_load_psi, 3)
    fos_ll = round(max_str / total_press, 2) if total_press > 0 else None

    storage_ok = (total_storage >= min_storage) if min_storage > 0 else None

    return {
        # Identity
        'tank_label':      tank_label,
        'config':          config,
        'layers':          layers,
        'traffic_load':    traffic_load,
        # Geometry
        'tank_width':      round(tank_width, 2),
        'tank_length':     round(tank_length, 2),
        'tank_height':     round(tank_height, 3),
        'tank_footprint':  round(tank_width * tank_length, 1),
        'used_perimeter':  round(used_perimeter, 2),
        'outer_width':     round(outer_width, 2),
        'outer_length':    round(outer_length, 2),
        'total_system_depth': round(total_system_depth, 3),
        'crates_wide':     crates_wide,
        'crates_long':     crates_long,
        'crates_layer':    crates_layer,
        'num_crates':      num_crates,
        # Storage
        'tank_storage':    round(tank_storage, 1),
        'stone_storage':   round(total_stone_stor, 1),
        'total_storage':   round(total_storage, 1),
        'min_storage':     min_storage,
        'storage_ok':      storage_ok,
        # BOM
        'base_units':      base_units,
        'side_plates':     side_plates,
        'bottom_plates':   bottom_plates,
        'pipe_connectors': pipe_connectors,
        'top_adapters_12': top_adapters_12,
        'top_adapters_16': top_adapters_16,
        'contingency':     contingency,
        # Geotextile
        'geoTank':   geoTank,
        'geoStone':  geoStone,
        'geoTotal':  geoTotal,
        'geoWaste':  geoWaste,
        # Stone backfill
        'stone_yd3':       stone_yd3,
        'stone_tons':      stone_tons,
        'stone_top_gross': stone_top_gross,  'stone_top_net':   stone_top_net,
        'stone_perim_gross': stone_perim_gross, 'stone_perim_net': stone_perim_net,
        'stone_base_gross': stone_base_gross,  'stone_base_net':  stone_base_net,
        # Elevations
        'surface_elev':    surface_elev,
        'tank_bottom_elev': tank_bottom_elev,
        'tank_top_elev':   round(tank_bottom_elev + tank_height, 3),
        'cover_depth':     cover_depth,
        'min_cover':       min_cover,
        'max_cover':       max_cover,
        'cover_ok':        cover_ok,
        'max_cover_ok':    max_cover_ok,
        # Loads
        'dead_load_psi':   dead_load_psi,
        'max_strength':    max_str,
        'fos_dead':        fos_dead,
        'll_psi':          ll_psi,
        'total_press':     total_press,
        'fos_ll':          fos_ll,
        # Stone inputs (for display)
        'perimeter_stone_width': perimeter_stone_width,
        'cover_stone':     cover_stone,
        'base_stone':      base_stone,
        'stone_void':      stone_void,
    }


def cumulative_bom(tank_results):
    """Sum BOM quantities across all tanks."""
    bom = {
        'base_units': 0, 'side_plates': 0, 'bottom_plates': 0,
        'pipe_connectors': 0, 'top_adapters_12': 0, 'top_adapters_16': 0,
        'contingency': 0,
        'tank_storage': 0.0, 'stone_storage': 0.0, 'total_storage': 0.0,
        'geoTank': 0.0, 'geoStone': 0.0, 'geoTotal': 0.0,
        'stone_yd3': 0.0, 'stone_tons': 0.0,
    }
    for r in tank_results:
        for k in bom:
            bom[k] = round(bom[k] + r.get(k, 0), 2)
    # Re-compute cumulative contingency as pallet-rounding of cumulative base_units
    bom['contingency'] = max(0,
        math.ceil(bom['base_units'] / PALLETS['base']) * PALLETS['base'] - bom['base_units'])
    return bom


# ══════════════════════════════════════════════════════════════════
#  ROUTE: /multi  —  main Multi-Tank page
# ══════════════════════════════════════════════════════════════════
@app.route('/multi', methods=['GET'])
def multi_index():
    return render_template('multi_tank.html')


# ══════════════════════════════════════════════════════════════════
#  ROUTE: /multi/calculate  —  JSON API called by JS
# ══════════════════════════════════════════════════════════════════
@app.route('/multi/calculate', methods=['POST'])
def multi_calculate():
    """
    Receives JSON array of tank dicts.
    Returns JSON with per-tank results + cumulative BOM.
    """
    try:
        data  = request.get_json(force=True)
        tanks = data.get('tanks', [])
        if not tanks:
            return jsonify({'error': 'No tanks provided'}), 400

        results = [calc_tank(t) for t in tanks]
        cum_bom  = cumulative_bom(results)

        # Weight & pallet calcs for cumulative BOM
        def bom_row(key, qty):
            w = round(qty * WEIGHTS.get(key, 0), 1)
            p = math.ceil(qty / PALLETS[key]) if qty > 0 and key in PALLETS else 0
            return {'qty': qty, 'weight_lbs': w, 'pallets': p}

        bom_detail = {
            'base_units':      bom_row('base',       cum_bom['base_units']),
            'side_plates':     bom_row('side',       cum_bom['side_plates']),
            'bottom_plates':   bom_row('bottom',     cum_bom['bottom_plates']),
            'pipe_connectors': bom_row('pipe',       cum_bom['pipe_connectors']),
            'top_adapters_12': bom_row('adapter12',  cum_bom['top_adapters_12']),
            'top_adapters_16': bom_row('adapter16',  cum_bom['top_adapters_16']),
            'contingency':     bom_row('base',       cum_bom['contingency']),
        }
        total_weight  = sum(v['weight_lbs'] for v in bom_detail.values())
        total_pallets = sum(v['pallets']     for v in bom_detail.values())

        return jsonify({
            'tank_results': results,
            'cumulative':   cum_bom,
            'bom_detail':   bom_detail,
            'total_weight': round(total_weight, 1),
            'total_pallets': total_pallets,
        })
    except Exception as exc:
        import traceback
        return jsonify({'error': str(exc), 'trace': traceback.format_exc()}), 500


# ══════════════════════════════════════════════════════════════════
#  ROUTE: /multi/download_quote  —  Multi-Tank Quote PDF
# ══════════════════════════════════════════════════════════════════
@app.route('/multi/download_quote', methods=['POST'])
def multi_download_quote():
    try:
        data = request.get_json(force=True)

        project_name    = data.get('project_name', 'Project')
        project_num     = data.get('project_num', '')
        location        = data.get('location', '')
        client          = data.get('client', '')
        estimator       = data.get('estimator', '')
        estimator_email = data.get('estimator_email', '')
        project_notes   = data.get('project_notes', '')
        cost_per_ft3    = float(data.get('cost_per_ft3', 0) or 0)
        freight_pct     = float(data.get('freight_pct', 10) or 10)

        tanks        = data.get('tanks', [])
        tank_results = [calc_tank(t) for t in tanks]
        cum          = cumulative_bom(tank_results)

        # Pricing
        tank_storage_total = cum['tank_storage']
        subtotal       = round(cost_per_ft3 * tank_storage_total, 2) if cost_per_ft3 > 0 else 0
        freight_cost   = round(subtotal * freight_pct / 100, 2)
        total_w_freight = round(subtotal + freight_cost, 2)

        def money(v):
            return f'${v:,.2f}'

        # Contingency recalc for display
        contingency_all = max(0,
            math.ceil(cum['base_units'] / PALLETS['base']) * PALLETS['base'] - cum['base_units'])

        generated_str = datetime.datetime.now().strftime('%m/%d/%Y')
        logo_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'static', 'aquacell-logo.png'
        )

        # ── PDF Canvas ──────────────────────────────────────────────
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=letter)
        W, H = letter

        # ── Quote helpers ────────────────────────────────────────────
        QNY  = colors.HexColor('#003366')
        QLGY = colors.HexColor('#f2f2f2')
        QMGY = colors.HexColor('#d0d0d0')
        QTBL = colors.HexColor('#2980b9')
        QYLW = colors.HexColor('#f7dc6f')

        LQ = 28
        RQ = 28
        QW = W - LQ - RQ

        def q_rule(cy, thick=0.5, clr=QMGY):
            c.setStrokeColor(clr)
            c.setLineWidth(thick)
            c.line(LQ, cy, LQ + QW, cy)

        def q_rect(x, y, w, h, fill, stroke=None, stroke_clr=None):
            c.setFillColor(fill)
            if stroke and stroke_clr:
                c.setStrokeColor(stroke_clr)
                c.setLineWidth(stroke)
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

        def new_page_header(title):
            nonlocal y
            c.showPage()
            y = H - 28
            q_rect(LQ, y - 30, QW, 30, QNY)
            q_text(W / 2, y - 20, title, 'Helvetica-Bold', 10, WHITE, 'center')
            q_text(LQ + 4, y - 20, project_name or '—', 'Helvetica', 8, colors.HexColor('#93c5fd'))
            q_text(LQ + QW - 4, y - 20, generated_str, 'Helvetica', 8, colors.HexColor('#94a3b8'), 'right')
            y -= 38

        # ════════════════════════════════════════
        #  PAGE 1 — HEADER
        # ════════════════════════════════════════
        y = H - 28

        # Logo
        if logo_path and os.path.exists(logo_path):
            try:
                img = ImageReader(logo_path)
                c.drawImage(img, LQ, y - 44, width=130, height=44,
                            preserveAspectRatio=True, mask='auto')
            except Exception:
                pass
        q_text(LQ + 4, y - 52, 'An Orbia business.', 'Helvetica-Oblique', 7, GRAY)

        q_text(W / 2, y - 14, 'MULTI-TANK MATERIALS QUOTE', 'Helvetica-Bold', 18, QNY, 'center')
        q_text(W / 2, y - 28, f'{len(tank_results)} Tank(s)  |  Combined AquaCell System', 'Helvetica', 9, GRAY, 'center')

        q_rect(W - RQ - 140, y - 52, 140, 52, QLGY)
        q_text(W - RQ - 70, y - 20, project_num if project_num else '—', 'Helvetica-Bold', 18, QNY, 'center')
        q_text(W - RQ - 70, y - 44, 'PROJECT #', 'Helvetica', 7, GRAY, 'center')

        y -= 62
        q_rule(y)
        y -= 4

        # Project info grid
        q_text(LQ, y - 8, 'CLIENT:', 'Helvetica-Bold', 8, QNY)
        q_text(LQ + 50, y - 8, client or '—', 'Helvetica', 8, BLACK)
        q_text(W / 2, y - 8, 'DATE', 'Helvetica-Bold', 8, QNY)
        q_text(W / 2 + 36, y - 8, generated_str, 'Helvetica', 8, BLACK)

        q_text(LQ, y - 20, 'PROJECT NAME:', 'Helvetica-Bold', 8, QNY)
        q_text(LQ + 80, y - 20, project_name or '—', 'Helvetica', 8, BLACK)
        q_text(W / 2, y - 20, 'PREPARED BY:', 'Helvetica-Bold', 8, QNY)
        q_text(W / 2 + 92, y - 20, estimator or '—', 'Helvetica', 8, BLACK)

        q_text(LQ, y - 32, 'LOCATION:', 'Helvetica-Bold', 8, QNY)
        q_text(LQ + 60, y - 32, location or '—', 'Helvetica', 8, BLACK)
        q_text(W / 2, y - 32, 'EMAIL:', 'Helvetica-Bold', 8, QNY)
        q_text(W / 2 + 52, y - 32, estimator_email or '—', 'Helvetica', 8, BLACK)

        y -= 44
        q_rule(y)
        y -= 8

        # Project notes
        if project_notes.strip():
            q_rect(LQ, y - 14, QW, 14, QNY)
            q_text(W / 2, y - 10, project_notes[:120], 'Helvetica-Bold', 7, WHITE, 'center')
            y -= 18

        # ════════════════════════════════════════
        #  STORAGE SUMMARY BANNER
        # ════════════════════════════════════════
        q_rect(LQ, y - 28, QW / 2, 28, QNY)
        q_text(LQ + 6, y - 10, 'TOTAL AQUACELL TANK STORAGE (ALL TANKS)', 'Helvetica-Bold', 7, WHITE)
        q_text(LQ + 6, y - 22, f'{cum["tank_storage"]:,.0f} FT³', 'Helvetica-Bold', 12, QYLW)

        q_rect(LQ + QW / 2, y - 28, QW / 2, 28, QTBL)
        q_text(LQ + QW / 2 + 6, y - 10, 'COMBINED SYSTEM TOTAL STORAGE (TANK + STONE)', 'Helvetica-Bold', 7, WHITE)
        q_text(LQ + QW / 2 + 6, y - 22, f'{cum["total_storage"]:,.0f} FT³', 'Helvetica-Bold', 12, WHITE)
        y -= 34

        # ════════════════════════════════════════
        #  PER-TANK SUMMARY TABLE
        # ════════════════════════════════════════
        q_rect(LQ, y - 14, QW, 14, QNY)
        q_text(W / 2, y - 10, 'PER-TANK SUMMARY', 'Helvetica-Bold', 8.5, WHITE, 'center')
        y -= 14

        # Table header
        col_lbl = LQ + 4
        col_cfg = LQ + 68
        col_dim = LQ + 130
        col_ht  = LQ + 235
        col_sto = LQ + 285
        col_bu  = LQ + 355
        col_sp  = LQ + 405
        col_bp  = LQ + 455
        col_cov = LQ + QW - 4

        hdr_cols = [
            ('TANK', col_lbl), ('CONFIG', col_cfg), ('W×L (ft)', col_dim),
            ('HT(ft)', col_ht), ('TANK FT³', col_sto),
            ('BASE', col_bu), ('SIDE', col_sp), ('BOT', col_bp), ('COVER', col_cov)
        ]
        q_rect(LQ, y - 13, QW, 13, colors.HexColor('#1a5276'))
        for lbl, x in hdr_cols:
            q_text(x, y - 9, lbl, 'Helvetica-Bold', 6.5, WHITE)
        y -= 13

        for i, r in enumerate(tank_results):
            shade = QLGY if i % 2 == 0 else WHITE
            cov_str = f"{r['cover_depth']}ft {'✓' if r['cover_ok'] else '✗'}"
            q_rect(LQ, y - 13, QW, 13, shade)
            q_text(col_lbl, y - 9, r['tank_label'][:14], 'Helvetica-Bold', 7, QNY)
            q_text(col_cfg, y - 9, f"{r['config']}-{r['layers']}", 'Helvetica', 7, BLACK)
            q_text(col_dim, y - 9, f"{r['tank_width']}×{r['tank_length']}", 'Helvetica', 7, BLACK)
            q_text(col_ht,  y - 9, f"{r['tank_height']}", 'Helvetica', 7, BLACK)
            q_text(col_sto, y - 9, f"{r['tank_storage']:,.0f}", 'Helvetica-Bold', 7, QNY)
            q_text(col_bu,  y - 9, str(r['base_units']), 'Helvetica', 7, BLACK)
            q_text(col_sp,  y - 9, str(r['side_plates']), 'Helvetica', 7, BLACK)
            q_text(col_bp,  y - 9, str(r['bottom_plates']), 'Helvetica', 7, BLACK)
            q_text(col_cov - 4, y - 9, cov_str, 'Helvetica', 7,
                   GREEN if r['cover_ok'] else RED, 'right')
            y -= 13

        # Totals row
        q_rect(LQ, y - 14, QW, 14, colors.HexColor('#1e3a8a'))
        q_text(col_lbl, y - 10, f'TOTAL ({len(tank_results)} TANKS)', 'Helvetica-Bold', 7, WHITE)
        q_text(col_sto, y - 10, f"{cum['tank_storage']:,.0f}", 'Helvetica-Bold', 8, QYLW)
        q_text(col_bu,  y - 10, str(cum['base_units']), 'Helvetica-Bold', 7, WHITE)
        q_text(col_sp,  y - 10, str(cum['side_plates']), 'Helvetica-Bold', 7, WHITE)
        q_text(col_bp,  y - 10, str(cum['bottom_plates']), 'Helvetica-Bold', 7, WHITE)
        y -= 20

        # ════════════════════════════════════════
        #  CUMULATIVE BOM SECTION
        # ════════════════════════════════════════
        q_rect(LQ, y - 14, QW, 14, QNY)
        q_text(W / 2, y - 10, 'CUMULATIVE BILL OF MATERIALS — AQUACELL COMPONENTS', 'Helvetica-Bold', 8.5, WHITE, 'center')
        y -= 14

        # BOM table header
        bom_col_ln  = LQ + 4
        bom_col_pc  = LQ + 32
        bom_col_ds  = LQ + 110
        bom_col_qt  = LQ + QW - 140
        bom_col_un  = LQ + QW - 80
        bom_col_wt  = LQ + QW - 4

        q_rect(LQ, y - 13, QW, 13, colors.HexColor('#1a5276'))
        for lbl, x in [('LINE', bom_col_ln), ('PART CODE', bom_col_pc), ('DESCRIPTION', bom_col_ds),
                        ('QTY', bom_col_qt), ('UNITS', bom_col_un), ('WEIGHT (lbs)', bom_col_wt)]:
            if lbl == 'WEIGHT (lbs)':
                q_text(x, y - 9, lbl, 'Helvetica-Bold', 6.5, WHITE, 'right')
            else:
                q_text(x, y - 9, lbl, 'Helvetica-Bold', 6.5, WHITE)
        y -= 13

        # Weight per unit lookup
        wt_map = {
            'base_units': ('base', 25.18), 'side_plates': ('side', 4.91),
            'bottom_plates': ('bottom', 7.86), 'pipe_connectors': ('pipe', 2.70),
            'top_adapters_12': ('adapter12', 11.02), 'top_adapters_16': ('adapter16', 22.00),
            'contingency': ('base', 25.18),
        }

        bom_rows = [
            ('1', '3091506',    'AQUACELL BASE UNIT',                             cum['base_units'],      'EACH', 'base_units'),
            ('2', '2476600003', 'AQUACELL SIDE PLATE',                            cum['side_plates'],     'EACH', 'side_plates'),
            ('3', '2476600001', 'AQUACELL BOTTOM PLATE',                          cum['bottom_plates'],   'EACH', 'bottom_plates'),
            ('4', '2476631200', 'AQUACELL 8\u201312\u2033 PIPE CONNECTOR',        cum['pipe_connectors'], 'EACH', 'pipe_connectors'),
            ('5', '3085857',    'AQUACELL TOP CONNECTOR (12\u2033)',              cum['top_adapters_12'], 'EACH', 'top_adapters_12'),
            ('5', '2476842000', 'AQUACELL TOP CONNECTOR (16\u2033)',              cum['top_adapters_16'], 'EACH', 'top_adapters_16'),
            ('6', '3091506',    '**CONTINGENCY BASE UNITS (FULL PALLET ROUNDING)', contingency_all,       'EACH', 'contingency'),
        ]

        total_weight_all = 0
        for i, (ln, pc, ds, qt, un, wkey) in enumerate(bom_rows):
            shade = QLGY if i % 2 == 0 else WHITE
            q_rect(LQ, y - 13, QW, 13, shade)
            _, wt_each = wt_map.get(wkey, ('', 0))
            row_wt = round(qt * wt_each, 1) if qt else 0
            total_weight_all += row_wt
            q_text(bom_col_ln, y - 9, ln, 'Helvetica', 7, BLACK)
            q_text(bom_col_pc, y - 9, pc, 'Helvetica', 7, BLACK)
            q_text(bom_col_ds, y - 9, ds, 'Helvetica', 7, BLACK)
            q_text(bom_col_qt + 30, y - 9, str(qt) if qt else '0', 'Helvetica-Bold', 7.5, QNY, 'right')
            q_text(bom_col_un, y - 9, un, 'Helvetica', 7, BLACK)
            q_text(bom_col_wt, y - 9, f'{row_wt:,.1f}', 'Helvetica', 7, GRAY, 'right')
            y -= 13

        # BOM weight total row
        q_rect(LQ, y - 13, QW, 13, colors.HexColor('#1e3a8a'))
        q_text(bom_col_ds, y - 9, 'COMBINED SYSTEM WEIGHT', 'Helvetica-Bold', 7.5, WHITE)
        q_text(bom_col_wt, y - 9, f'{total_weight_all:,.1f} lbs', 'Helvetica-Bold', 8, QYLW, 'right')
        y -= 18

        # ════════════════════════════════════════
        #  PRICING BLOCK
        # ════════════════════════════════════════
        if cost_per_ft3 > 0:
            q_rect(LQ, y - 13, QW - 120, 13, QLGY)
            q_text(LQ + 4, y - 9,
                   f'Pricing: ${cost_per_ft3:.4f}/ft³ × {tank_storage_total:,.1f} ft³ combined AquaCell storage',
                   'Helvetica-Oblique', 7, GRAY)
            q_rect(LQ + QW - 120, y - 13, 120, 13, QNY)
            q_text(LQ + QW - 4, y - 9, 'AQUACELL SUB-TOTAL', 'Helvetica-Bold', 7, WHITE, 'right')
            y -= 13
            q_rect(LQ + QW - 120, y - 13, 120, 13, QLGY)
            q_text(LQ + QW - 4, y - 9, money(subtotal), 'Helvetica-Bold', 8.5, QNY, 'right')
            y -= 18

        y -= 6
        totals_rows = [
            ('AQUACELL SUB-TOTAL',                money(subtotal),           QNY,  QLGY),
            ('ESTIMATED TAXES*',                  '$0.00  (TBD at purchase)',GRAY,  WHITE),
            (f'ESTIMATED FREIGHT* ({freight_pct:.1f}%)', money(freight_cost), colors.HexColor('#784212'), colors.HexColor('#fef9e7')),
            ('ESTIMATED TOTAL',                   money(total_w_freight),   QGRN,  colors.HexColor('#eafaf1')),
        ]
        tot_lbl_w = 160
        tot_val_w = 110
        tx_lbl = LQ + QW - tot_lbl_w - tot_val_w - 4
        tx_val = LQ + QW - tot_val_w

        for lbl, val, fg, bg in totals_rows:
            q_rect(tx_lbl - 4, y - 15, tot_lbl_w + tot_val_w + 8, 15, bg)
            q_text(tx_lbl + tot_lbl_w - 4, y - 5, lbl, 'Helvetica-Bold', 8, fg, 'right')
            q_text(tx_val + tot_val_w - 4,  y - 5, val, 'Helvetica-Bold', 9, fg, 'right')
            q_rule(y - 15, thick=0.3, clr=QMGY)
            y -= 15

        y -= 8

        # Footnotes
        for note in [
            '*ESTIMATED TAXES & FREIGHT TO BE DETERMINED AT TIME OF PURCHASE',
            '*THIS QUOTE IS VALID FOR 30 DAYS FROM THE DATE OF ISSUANCE. SUBJECT TO CHANGE AFTER THIS DATE',
        ]:
            q_text(W / 2, y, note, 'Helvetica-Bold', 7, QRED, 'center')
            y -= 11

        y -= 6

        # Disclaimer
        disc = (
            "Disclaimer: This calculator provides preliminary, conceptual estimates only. "
            "Wavin's assistance in sizing or product selection is advisory and does not constitute "
            "design responsibility or guarantee system performance. The Engineer of Record (EoR) is "
            "solely responsible for verifying all design parameters. AquaCell dimensions and "
            "assumptions follow published product data. FINAL LAYOUTS, CAPACITIES, AND INSTALLATION "
            "DEPTHS MUST BE CONFIRMED BY A LICENSED PROFESSIONAL ENGINEER."
        )
        c.setFillColor(GRAY)
        c.setFont('Helvetica', 6.5)
        disc_words = disc.split()
        dlines, dline = [], ''
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

        # ════════════════════════════════════════
        #  PAGE 2+  — PER-TANK DETAIL PAGES
        # ════════════════════════════════════════
        for ti, r in enumerate(tank_results):
            new_page_header(f"Tank Detail: {r['tank_label']}  ({r['config']}-{r['layers']})")

            def detail_row(label, value, shade=False):
                nonlocal y
                rh = 13
                if shade:
                    c.setFillColor(LGRAY)
                    c.rect(LQ, y - rh + 3, QW, rh, fill=1, stroke=0)
                c.setFillColor(GRAY)
                c.setFont('Helvetica', 7.5)
                c.drawString(LQ + 5, y - 3, label)
                c.setFillColor(BLACK)
                c.setFont('Helvetica-Bold', 7.5)
                c.drawRightString(LQ + QW - 5, y - 3, str(value))
                c.setStrokeColor(MGRAY)
                c.setLineWidth(0.25)
                c.line(LQ, y - rh + 3, LQ + QW, y - rh + 3)
                y -= rh

            def section_hdr(title):
                nonlocal y
                bh = 15
                c.setFillColor(NAVY)
                c.rect(LQ, y - bh + 4, QW, bh, fill=1, stroke=0)
                c.setFillColor(WHITE)
                c.setFont('Helvetica-Bold', 8)
                c.drawString(LQ + 6, y - bh + 8, title)
                y -= bh + 2

            section_hdr('■  STORAGE')
            detail_row('AquaCell Tank Storage', f"{r['tank_storage']:,.1f} ft³", shade=False)
            detail_row('Stone Backfill Storage', f"{r['stone_storage']:,.1f} ft³", shade=True)
            detail_row('Total System Storage', f"{r['total_storage']:,.1f} ft³", shade=False)
            if r['min_storage'] > 0:
                status = '✓ PASS' if r['storage_ok'] else '✗ FAIL'
                detail_row(f"Minimum Required Storage ({r['min_storage']:,.0f} ft³)", status, shade=True)
            y -= 5

            section_hdr('■  GEOMETRY')
            detail_row('Snapped Tank (W × L)', f"{r['tank_width']} ft × {r['tank_length']} ft", shade=False)
            detail_row('Crates per Layer', f"{r['crates_wide']} × {r['crates_long']} = {r['crates_layer']}", shade=True)
            detail_row('Number of Layers', str(r['layers']), shade=False)
            detail_row('Tank Height', f"{r['tank_height']} ft", shade=True)
            detail_row('Total System Depth', f"{r['total_system_depth']} ft", shade=False)
            detail_row('Tank Perimeter', f"{r['used_perimeter']} ft", shade=True)
            y -= 5

            section_hdr('■  COVER & LOAD')
            detail_row('Surface Elevation', f"{r['surface_elev']} ft", shade=False)
            detail_row('Tank Bottom Elevation', f"{r['tank_bottom_elev']} ft", shade=True)
            detail_row('Tank Top Elevation', f"{r['tank_top_elev']} ft", shade=False)
            cov_label = f"Cover Depth (min {r['min_cover']} ft)"
            cov_val   = f"{r['cover_depth']} ft — {'PASS' if r['cover_ok'] else 'FAIL'}"
            detail_row(cov_label, cov_val, shade=True)
            detail_row('Factor of Safety — Dead Load', str(r['fos_dead'] or '—'), shade=False)
            detail_row('Factor of Safety — Live Load', str(r['fos_ll'] or '—'), shade=True)
            y -= 5

            section_hdr('■  BILL OF MATERIALS (THIS TANK)')
            detail_row('Base Units (3091506)', str(r['base_units']), shade=False)
            detail_row('Side Plates (2476600003)', str(r['side_plates']), shade=True)
            detail_row('Bottom Plates (2476600001)', str(r['bottom_plates']), shade=False)
            detail_row('8-12" Pipe Connectors (2476631200)', str(r['pipe_connectors']), shade=True)
            detail_row('12" Top Adapters (3085857)', str(r['top_adapters_12']), shade=False)
            detail_row('16" Top Adapters (2476842000)', str(r['top_adapters_16']), shade=True)

        c.save()
        buffer.seek(0)

        safe_name = (project_name or 'Project').strip().replace(' ', '_')
        safe_num  = (project_num or '').strip().replace(' ', '_')
        name_part = '_'.join(filter(None, [safe_num, safe_name]))
        return send_file(
            buffer, as_attachment=True,
            download_name=f'AquaCell_MultiTank_Quote_{name_part}_{datetime.datetime.now().strftime("%m%d%Y")}.pdf',
            mimetype='application/pdf'
        )

    except Exception as exc:
        import traceback
        return jsonify({'error': str(exc), 'trace': traceback.format_exc()}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5001)
