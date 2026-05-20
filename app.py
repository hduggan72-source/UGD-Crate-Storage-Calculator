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

# ── ASTM F2787 Live Load Model constants ──
_LL_m    = 1.2      # Multiple Presence Factor
_LL_IM   = 0.2475   # Dynamic Allowance Factor (24.75%)
_LL_LLDF = 1.15     # Live Load Distribution Factor (select granular fill)

def _ll_wheel(traffic_load):
    """Wheel load and tire contact dims by traffic type."""
    return {'H10': (8000, 10, 10), 'HS20': (16000, 10, 20), 'HS25': (20000, 10, 20)}.get(traffic_load, (16000, 10, 20))

def calc_live_load_fos(traffic_load, cover_depth_ft, config):
    """
    Calculate live load FoS per ASTM F2787 / AquaCell Load Model.
    FoS_LL = max_compressive / (factored_ll_psi + dead_load_psi)
    FoS_DL = max_compressive / dead_load_psi
    """
    if cover_depth_ft <= 0:
        return None, None, None, None
    cover_in = cover_depth_ft * 12
    wl, tire_L, tire_W = _ll_wheel(traffic_load)
    # Projected area using 2V:1H distribution with LLDF
    proj_area_in2 = (tire_L + cover_in * _LL_LLDF) * (tire_W + cover_in * _LL_LLDF)
    # Factored live load (lbs) = dynamic+presence adjusted wheel load + minor shear term
    ll_lbs = wl * _LL_m * (1 + _LL_IM) + wl / 180.0
    ll_psi = ll_lbs / proj_area_in2
    dl_psi = (cover_in / 12.0) * 120 / 144   # cover weight at 120 pcf
    max_str = 70 if config == 'SC' else 100   # psi
    fos_ll = round(max_str / (ll_psi + dl_psi), 2) if (ll_psi + dl_psi) > 0 else None
    fos_dl = round(max_str / dl_psi, 2) if dl_psi > 0 else None
    return round(ll_psi, 3), round(dl_psi, 3), fos_ll, fos_dl

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
        top_adapters_16 = int(request.form.get('top_adapters_16', 0) or 0)
        project_notes = request.form.get('project_notes', '')
        include_stage_storage = request.form.get('include_stage_storage') == 'yes'
        include_schematic = request.form.get('include_schematic') == 'yes'
        stage_increment_in = int(request.form.get('stage_increment_in', 12) or 12)

        shape_mode = request.form.get('shape_mode', 'rectangle')

        # ── Crate module constants ──
        MODULE_WID = 1.9685   # 23.6 in → ft
        MODULE_LEN = 3.937    # 47.2 in → ft
        SIDE_PLATE_FT = 23.6 / 12  # 1.9667 ft per side plate slot

        if config == 'SC':
            layer_heights = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
            void_ratio = 0.95486
            side_multiplier = 1.312336
        else:
            layer_heights = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
            void_ratio = 0.92633
            side_multiplier = 1.509186351

        tank_height = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]
        total_system_depth = base_stone + tank_height + cover_stone

        if shape_mode == 'complex':
            # ── COMPLEX SHAPE MODE ──
            # User provides: scaled area, scaled perimeter, and ONE known dimension from plans.
            # We derive the other dimension, then snap BOTH independently to crate module grids.
            # This matches the Excel complex shape calculator logic exactly.
            complex_scaled_area  = float(request.form.get('complex_scaled_area', 0) or 0)
            complex_tank_perim   = float(request.form.get('complex_tank_perim', 0) or 0)
            complex_known_dim    = float(request.form.get('complex_known_dim', 0) or 0)
            complex_excav_area   = float(request.form.get('complex_excav_area', 0) or 0)
            complex_excav_perim  = float(request.form.get('complex_excav_perim', 0) or 0)

            # Step 1: derive the other dimension from scaled area ÷ known dimension
            if complex_known_dim > 0 and complex_scaled_area > 0:
                other_dim = complex_scaled_area / complex_known_dim
            else:
                other_dim = 0

            # Step 2: snap each dimension to its crate module grid
            # The known dimension snaps along MODULE_WID (23.6") rows
            # The derived dimension snaps along MODULE_LEN (47.2") columns
            crates_known = math.floor(complex_known_dim / MODULE_WID) if complex_known_dim > 0 else 0
            crates_other = math.floor(other_dim / MODULE_LEN) if other_dim > 0 else 0
            snapped_known = crates_known * MODULE_WID
            snapped_other = crates_other * MODULE_LEN

            # Step 3: real-world snapped area (always ≤ scaled area)
            complex_tank_area = snapped_known * snapped_other
            crates_per_layer  = crates_known * crates_other
            num_crates        = crates_per_layer * layers

            # Storage from snapped area
            gross_tank_vol = complex_tank_area * tank_height
            tank_storage   = gross_tank_vol * void_ratio

            # Side plates from SCALED perimeter (user-measured, not snapped)
            tank_perim_calc = complex_tank_perim
            side_plates = round(tank_perim_calc * (layers * side_multiplier) / 5.17)

            if config == 'SC':
                base_units    = num_crates
                bottom_plates = crates_per_layer
            else:
                base_units    = num_crates * 2
                bottom_plates = 0

            # Excavation envelope — use user values or auto-estimate.
            # Area  : rectangle bounding box (snapped_W + 2×stone) × (snapped_L + 2×stone)
            # Perim : scaled plan perimeter + 8×stone (1ft offset on each side of complex shape)
            # These match the Excel complex shape calculator exactly.
            if complex_excav_area <= 0:
                complex_excav_area = (snapped_known + 2 * perimeter_stone_width) * (snapped_other + 2 * perimeter_stone_width)
            if complex_excav_perim <= 0:
                complex_excav_perim = complex_tank_perim + 8 * perimeter_stone_width

            total_excavation_vol  = complex_excav_area * total_system_depth
            stone_envelope_volume = total_excavation_vol - gross_tank_vol
            total_stone_storage   = stone_envelope_volume * stone_void
            total_storage         = tank_storage + total_stone_storage

            # Geotextile — matches Excel exactly:
            # Tank: 2×snapped_area + scaled_perim×tank_height
            # Stone envelope: 2×excav_area + excav_perim×total_system_depth
            geoTank  = round((2 * complex_tank_area + complex_tank_perim * tank_height) * (1 + geoWaste / 100.0), 1)
            geoStone = round((2 * complex_excav_area + complex_excav_perim * total_system_depth) * (1 + geoWaste / 100.0), 1)
            geoTotal = round(geoTank + geoStone, 1)

            used_perimeter  = round(complex_tank_perim, 2)
            tank_width      = round(snapped_known, 2)
            tank_length     = round(snapped_other, 2)

        else:
            # ── RECTANGLE MODE (existing logic) ──
            crates_wide = math.floor(known_width / MODULE_WID)
            crates_long = math.floor(known_length / MODULE_LEN)
            tank_width  = crates_wide * MODULE_WID
            tank_length = crates_long * MODULE_LEN

            num_crates = crates_wide * crates_long * layers
            gross_tank_vol = num_crates * (MODULE_WID * MODULE_LEN * tank_height / layers)
            tank_storage = gross_tank_vol * void_ratio

            outer_width  = tank_width  + 2 * perimeter_stone_width
            outer_length = tank_length + 2 * perimeter_stone_width
            total_excavation_vol = outer_width * outer_length * total_system_depth
            stone_envelope_volume = total_excavation_vol - gross_tank_vol
            total_stone_storage = stone_envelope_volume * stone_void
            total_storage = tank_storage + total_stone_storage

            used_perimeter = 2 * (tank_width + tank_length)
            tank_perim_calc = float(tank_perimeter) if tank_perimeter else used_perimeter
            side_plates = round(tank_perim_calc * (layers * side_multiplier) / 5.17)

            if config == 'SC':
                base_units   = num_crates
                bottom_plates = crates_wide * crates_long
            else:
                base_units   = num_crates * 2
                bottom_plates = 0

            tank_top_bottom_area = 2 * tank_width * tank_length
            tank_sides_area = used_perimeter * tank_height
            geoTank  = round((tank_top_bottom_area + tank_sides_area) * (1 + geoWaste / 100.0), 1)
            geoStone = round((outer_width * outer_length * 2 + outer_width * total_system_depth * 2 + outer_length * total_system_depth * 2) * (1 + geoWaste / 100.0), 1)
            geoTotal = round(geoTank + geoStone, 1)

            complex_tank_area   = None
            complex_tank_perim  = None
            complex_excav_area  = None
            complex_excav_perim = None

        stone_backfill_bulk_ft3  = round(stone_envelope_volume * 1.10, 1)
        stone_backfill_bulk_yd3  = round(stone_backfill_bulk_ft3 / 27, 2)
        stone_backfill_bulk_tons = round(stone_backfill_bulk_ft3 * 100 / 2000, 2)

        # ── Stone Backfill by Layer (Gross / Net) ──
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
        stone_layer_total_net   = round(stone_top_net  + stone_perim_net  + stone_base_net,  1)

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

        if traffic_load == 'H10':
            min_cover_req = 1.0
        elif traffic_load == 'HS20':
            min_cover_req = 1.5 if config == 'SC' else 1.33
        elif traffic_load == 'HS25':
            min_cover_req = 2.5 if config == 'SC' else 1.83
        else:
            min_cover_req = 1.0

        # UPDATED MAXIMUM ALLOWABLE COVER DEPTH
        max_cover_req = 14.4 if config == 'SC' else 26.2
        cover_status = 'PASS' if cover_depth >= min_cover_req else 'FAIL'

        dead_load_psi = round(cover_stone * 120 / 144, 2)
        max_compressive = 70 if config == 'SC' else 100
        fos_dead = round(max_compressive / dead_load_psi, 2) if dead_load_psi > 0 else None

        # Live load FoS (ASTM F2787 / AquaCell Load Model)
        ll_psi, dl_psi_check, fos_live_load, fos_dead_check = calc_live_load_fos(traffic_load, cover_depth, config)

        storage_status = 'PASS' if min_storage <= total_storage else 'FAIL' if min_storage > 0 else None

        results = {
            'config': config,
            'layers': layers,
            'surface_elev': surface_elev,
            'tank_bottom_elev': tank_bottom_elev,
            'tank_top_elev': round(tank_bottom_elev + tank_height, 2),
            'cover_depth': cover_depth,
            'cover_status': cover_status,
            'max_cover_req': max_cover_req,
            'max_cover_status': 'PASS' if cover_depth <= max_cover_req else 'FAIL',
            'dead_load_psi': dead_load_psi,
            'fos_dead': fos_dead,
            'll_psi': ll_psi,
            'fos_live_load': fos_live_load,
            'traffic_load': traffic_load,
            'tank_width': round(tank_width, 2),
            'tank_length': round(tank_length, 2),
            'tank_height': round(tank_height, 2),
            'tank_storage': round(tank_storage, 1),
            'stone_storage': round(total_stone_storage, 1),
            'total_storage': round(total_storage, 1),
            'shape_mode': shape_mode,
            'complex_scaled_area': complex_scaled_area if shape_mode == 'complex' else None,
            'complex_tank_area': round(complex_tank_area, 2) if shape_mode == 'complex' else None,
            'complex_tank_perim': complex_tank_perim if shape_mode == 'complex' else None,
            'complex_known_dim': complex_known_dim if shape_mode == 'complex' else None,
            'complex_snapped_known': round(snapped_known, 3) if shape_mode == 'complex' else None,
            'complex_snapped_other': round(snapped_other, 3) if shape_mode == 'complex' else None,
            'complex_crates_known': crates_known if shape_mode == 'complex' else None,
            'complex_crates_other': crates_other if shape_mode == 'complex' else None,
            'complex_excav_area': round(complex_excav_area, 1) if shape_mode == 'complex' else None,
            'complex_excav_perim': round(complex_excav_perim, 1) if shape_mode == 'complex' else None,
            'used_perimeter': round(used_perimeter, 2),
            'base_units': base_units,
            'side_plates': side_plates,
            'bottom_plates': bottom_plates,
            'pipe_connectors': pipe_connectors,
            'top_adapters_12': top_adapters_12,
            'top_adapters_16': top_adapters_16,
            'project_notes': project_notes,
            'stage_storage': stage_storage,
            'stage_increment_in': stage_increment_in,
            'min_storage': min_storage if min_storage > 0 else None,
            'storage_status': storage_status,
            'stone_backfill_bulk_ft3': stone_backfill_bulk_ft3,
            'stone_backfill_bulk_yd3': stone_backfill_bulk_yd3,
            'stone_backfill_bulk_tons': stone_backfill_bulk_tons,
            'stone_top_gross': stone_top_gross,
            'stone_top_net': stone_top_net,
            'stone_perim_gross': stone_perim_gross,
            'stone_perim_net': stone_perim_net,
            'stone_base_gross': stone_base_gross,
            'stone_base_net': stone_base_net,
            'stone_layer_total_gross': stone_layer_total_gross,
            'stone_layer_total_net': stone_layer_total_net,
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
    known_width = float(request.form.get('known_width', 0) or 0)
    known_length = float(request.form.get('known_length', 0) or 0)
    perimeter_stone_width = float(request.form.get('perimeter_stone_width', 1.0) or 1.0)
    cover_stone = float(request.form.get('cover_stone', 1.0) or 1.0)
    base_stone = float(request.form.get('base_stone', 0.333) or 0.333)
    stone_void = float(request.form.get('stone_void', 0.40) or 0.40)
    geoWaste = int(request.form.get('geoWaste', 10) or 10)
    pipe_connectors = int(request.form.get('pipe_connectors', 0) or 0)
    top_adapters_12 = int(request.form.get('top_adapters_12', 0) or 0)
    top_adapters_16 = int(request.form.get('top_adapters_16', 0) or 0)
    project_notes = request.form.get('project_notes', '')
    min_storage = float(request.form.get('min_storage', 0) or 0)
    include_stage_storage = request.form.get('include_stage_storage') == 'yes'
    include_schematic = request.form.get('include_schematic') == 'yes'
    schematic_image = request.form.get('schematic_image')
    stage_increment_in = int(request.form.get('stage_increment_in', 12) or 12)

    shape_mode = request.form.get('shape_mode', 'rectangle')

    MODULE_WID = 1.9685
    MODULE_LEN = 3.937

    if config == 'SC':
        layer_heights = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
        void_ratio = 0.95486
        side_multiplier = 1.312336
    else:
        layer_heights = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
        void_ratio = 0.92633
        side_multiplier = 1.509186351

    tank_height = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]
    total_system_depth = base_stone + tank_height + cover_stone

    if shape_mode == 'complex':
        complex_scaled_area  = float(request.form.get('complex_scaled_area', 0) or 0)
        complex_tank_perim   = float(request.form.get('complex_tank_perim', 0) or 0)
        complex_known_dim    = float(request.form.get('complex_known_dim', 0) or 0)
        complex_excav_area   = float(request.form.get('complex_excav_area', 0) or 0)
        complex_excav_perim  = float(request.form.get('complex_excav_perim', 0) or 0)

        other_dim     = complex_scaled_area / complex_known_dim if complex_known_dim > 0 else 0
        crates_known  = math.floor(complex_known_dim / MODULE_WID) if complex_known_dim > 0 else 0
        crates_other  = math.floor(other_dim / MODULE_LEN) if other_dim > 0 else 0
        snapped_known = crates_known * MODULE_WID
        snapped_other = crates_other * MODULE_LEN
        complex_tank_area = snapped_known * snapped_other
        crates_per_layer  = crates_known * crates_other
        num_crates        = crates_per_layer * layers

        gross_tank_vol  = complex_tank_area * tank_height
        tank_storage    = gross_tank_vol * void_ratio
        tank_perim_calc = complex_tank_perim
        side_plates     = round(tank_perim_calc * (layers * side_multiplier) / 5.17)

        if config == 'SC':
            base_units    = num_crates
            bottom_plates = crates_per_layer
        else:
            base_units    = num_crates * 2
            bottom_plates = 0

        if complex_excav_area <= 0:
            complex_excav_area = (snapped_known + 2 * perimeter_stone_width) * (snapped_other + 2 * perimeter_stone_width)
        if complex_excav_perim <= 0:
            complex_excav_perim = complex_tank_perim + 8 * perimeter_stone_width

        total_excavation_vol  = complex_excav_area * total_system_depth
        stone_envelope_volume = total_excavation_vol - gross_tank_vol
        total_stone_storage   = stone_envelope_volume * stone_void
        total_storage         = tank_storage + total_stone_storage
        used_perimeter        = complex_tank_perim

        geoTank  = round((2 * complex_tank_area + complex_tank_perim * tank_height) * (1 + geoWaste / 100.0), 1)
        geoStone = round((2 * complex_excav_area + complex_excav_perim * total_system_depth) * (1 + geoWaste / 100.0), 1)
        geoTotal = round(geoTank + geoStone, 1)

        tank_width  = round(snapped_known, 2)
        tank_length = round(snapped_other, 2)

    else:
        crates_wide = math.floor(known_width / MODULE_WID)
        crates_long = math.floor(known_length / MODULE_LEN)
        tank_width  = crates_wide * MODULE_WID
        tank_length = crates_long * MODULE_LEN

        num_crates    = crates_wide * crates_long * layers
        gross_tank_vol = num_crates * (MODULE_WID * MODULE_LEN * tank_height / layers)
        tank_storage   = gross_tank_vol * void_ratio

        outer_width  = tank_width  + 2 * perimeter_stone_width
        outer_length = tank_length + 2 * perimeter_stone_width
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

        tank_top_bottom_area = 2 * tank_width * tank_length
        tank_sides_area      = used_perimeter * tank_height
        geoTank  = round((tank_top_bottom_area + tank_sides_area) * (1 + geoWaste / 100.0), 1)
        geoStone = round((outer_width * outer_length * 2 + outer_width * total_system_depth * 2 + outer_length * total_system_depth * 2) * (1 + geoWaste / 100.0), 1)
        geoTotal = round(geoTank + geoStone, 1)

        complex_tank_area = complex_tank_perim = complex_excav_area = complex_excav_perim = None

    stone_backfill_bulk_ft3  = round(stone_envelope_volume * 1.10, 1)
    stone_backfill_bulk_yd3  = round(stone_backfill_bulk_ft3 / 27, 2)
    stone_backfill_bulk_tons = round(stone_backfill_bulk_ft3 * 100 / 2000, 2)

    # ── Stone Backfill by Layer (Gross / Net) ──
    if shape_mode == 'complex':
        excav_area_for_layers = complex_excav_area
        tank_footprint        = complex_tank_area
    else:
        excav_area_for_layers = outer_width * outer_length
        tank_footprint        = tank_width * tank_length

    stone_top_gross         = round(excav_area_for_layers * cover_stone, 1)
    stone_top_net           = round(stone_top_gross * stone_void, 1)
    stone_perim_gross       = round((excav_area_for_layers - tank_footprint) * tank_height, 1)
    stone_perim_net         = round(stone_perim_gross * stone_void, 1)
    stone_base_gross        = round(excav_area_for_layers * base_stone, 1)
    stone_base_net          = round(stone_base_gross * stone_void, 1)
    stone_layer_total_gross = round(stone_top_gross + stone_perim_gross + stone_base_gross, 1)
    stone_layer_total_net   = round(stone_top_net  + stone_perim_net  + stone_base_net,  1)

    storage_status = 'PASS' if min_storage <= total_storage else 'FAIL' if min_storage > 0 else None

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

    cover_depth = round(surface_elev - (tank_bottom_elev + tank_height), 2)

    if traffic_load == 'H10':
        min_cover_req = 1.0
    elif traffic_load == 'HS20':
        min_cover_req = 1.5 if config == 'SC' else 1.33
    elif traffic_load == 'HS25':
        min_cover_req = 2.5 if config == 'SC' else 1.83
    else:
        min_cover_req = 1.0

    # UPDATED MAXIMUM ALLOWABLE COVER DEPTH (per your request)
    max_cover_req = 14.4 if config == 'SC' else 26.2
    cover_status = 'PASS' if cover_depth >= min_cover_req else 'FAIL'

    dead_load_psi = round(cover_stone * 120 / 144, 2)
    max_compressive = 70 if config == 'SC' else 100
    fos_dead = round(max_compressive / dead_load_psi, 2) if dead_load_psi > 0 else None

    # Live load FoS
    ll_psi_pdf, _, fos_live_load_pdf, _ = calc_live_load_fos(traffic_load, cover_depth, config)

    # Build PDF
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 70

    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if os.path.exists(logo_path):
        img = ImageReader(logo_path)
        c.drawImage(img, 50, y - 30, width=180, height=60, preserveAspectRatio=True, mask='auto')

    c.setFont("Helvetica", 9)
    timestamp = f"Generated {datetime.datetime.now().strftime('%m/%d/%Y %H:%M')}"
    c.drawRightString(width - 50, y - 10, timestamp)

    y -= 90

    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, y, "AquaCell V14 Crate Calculator")
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
    shape_label = "Complex Shape" if shape_mode == 'complex' else "Rectangle"
    c.drawString(50, y, f"Project Name: {project_name}   Configuration: {config} ({layers} Layers) — {shape_label} Mode")
    y -= 18
    c.drawString(50, y, f"Surface Elevation: {surface_elev} ft")
    y -= 18
    c.drawString(50, y, f"Tank Bottom Elevation: {tank_bottom_elev} ft")
    y -= 18
    c.drawString(50, y, f"Tank Top Elevation: {round(tank_bottom_elev + tank_height, 2)} ft")
    y -= 18

    c.drawString(50, y, f"Actual Cover Depth: {cover_depth} ft → {cover_status}")
    y -= 18
    c.drawString(50, y, f"Maximum Allowable Cover Depth: {max_cover_req} ft → {'PASS' if cover_depth <= max_cover_req else 'FAIL'}")
    y -= 18
    c.drawString(50, y, f"Traffic Load: {traffic_load}   |   Dead Load Pressure: {dead_load_psi} psi   |   Live Load Pressure: {ll_psi_pdf} psi")
    y -= 18
    c.drawString(50, y, f"Factor of Safety — Dead Load: {fos_dead}   |   Factor of Safety — Live Load ({traffic_load}): {fos_live_load_pdf if fos_live_load_pdf else 'N/A'}")
    y -= 25

    lines = [
        (f"Complex Shape — Scaled Area: {complex_scaled_area} ft²   Perimeter: {complex_tank_perim} ft   Known Dim: {complex_known_dim} ft"
         if shape_mode == 'complex' else
         f"Snapped Tank: {round(tank_width,2)} ft × {round(tank_length,2)} ft × {round(tank_height,2)} ft"),
        (f"Real-World Snapped Tank: {round(tank_width,2)} ft × {round(tank_length,2)} ft × {round(tank_height,2)} ft   (Snapped Area: {round(complex_tank_area,1)} ft²)"
         if shape_mode == 'complex' else ""),
        (f"Excavation Envelope: {round(complex_excav_area,1)} ft²   Perimeter: {round(complex_excav_perim,1)} ft"
         if shape_mode == 'complex' else ""),
        f"AquaCell Tank Storage: {round(tank_storage,1)} ft³",
        f"Stone Storage: {round(total_stone_storage,1)} ft³",
        f"Total System Storage: {round(total_storage,1)} ft³",
        f"Minimum Required Storage Volume: {min_storage} ft³ → {storage_status}" if min_storage > 0 else "",
        "",
        f"Estimated Stone Backfill Volume (with 10% added): {stone_backfill_bulk_ft3} ft³ / {stone_backfill_bulk_yd3} yd³ / {stone_backfill_bulk_tons} US tons",
        "  Stone Backfill by Layer (Gross Volume / Net Storage @ void ratio):",
        f"    Top Layer:       {stone_top_gross} ft³ gross  /  {stone_top_net} ft³ net",
        f"    Perimeter:       {stone_perim_gross} ft³ gross  /  {stone_perim_net} ft³ net",
        f"    Base Layer:      {stone_base_gross} ft³ gross  /  {stone_base_net} ft³ net",
        f"    TOTAL:           {stone_layer_total_gross} ft³ gross  /  {stone_layer_total_net} ft³ net",
        "  * Stone backfill provided by others. Quantity is an estimate for coordination purposes only.",
        "",
        "Bill of Materials",
        f"Base Unit (3091506) ................ {base_units}",
        f"Side Plate (2476600003) ............ {side_plates}",
        f"Bottom Plate (2476600001) .......... {bottom_plates}",
        f"8-12\" Pipe Connectors (2476631200) ............... {pipe_connectors}",
        f"12\" Top Adapters (3085857) .................... {top_adapters_12}",
        f"16\" Top Adapters (2476842000) .................... {top_adapters_16}",
        "",
        "Geotextile Fabric (provided by others — quantities are estimates for coordination purposes only)",
        f"AquaCell Tank Only .................... {geoTank} ft² ({round(geoTank/9,1)} yd²)",
        f"Stone Backfill Envelope .................... {geoStone} ft² ({round(geoStone/9,1)} yd²)",
        f"Total Geotextile .................... {geoTotal} ft² ({round(geoTotal/9,1)} yd²)",
        f"Waste/Overlap .................... {geoWaste}.0%",
    ]

    for line in lines:
        if line:
            c.drawString(50, y, line)
            y -= 18

    # Project Notes with text wrapping
    if project_notes.strip():
        c.setFont("Helvetica", 9)
        c.drawString(50, y, "Project Notes / Special Instructions / Assumptions:")
        y -= 18
        words = project_notes.split()
        line = ""
        for word in words:
            if len(line) + len(word) > 90:
                c.drawString(50, y, line)
                y -= 14
                line = word + " "
            else:
                line += word + " "
        if line:
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

    # Schematic as LAST page
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
    return send_file(buffer, as_attachment=True, download_name="AquaCell_V14_Report.pdf", mimetype='application/pdf')

if __name__ == '__main__':
    app.run(debug=True)
