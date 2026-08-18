from flask import Flask, render_template, request, send_file, jsonify
import io
import json
import re
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
import datetime
import math
import os
import base64
from io import BytesIO
import requests
from pypdf import PdfWriter, PdfReader as PyPdfReader

app = Flask(__name__)

# ── Module-level crate dimensions (used by all calc functions) ──
MODULE_WID = 1.9685   # ft
MODULE_LEN = 3.937    # ft

# ══════════════════════════════════════════════════════════════════
#  GITHUB DETAIL SHEET CONFIG  — update GITHUB_BRANCH when you
#  cut a new branch; everything else stays the same.
# ══════════════════════════════════════════════════════════════════
GITHUB_OWNER  = "hduggan72-source"
GITHUB_REPO   = "UGD-Crate-Storage-Calculator"
GITHUB_BRANCH = "Pricing_Engine_v1"
GITHUB_FOLDER = "Details"
GITHUB_PAT    = os.environ.get("GITHUB_PAT", "")   # set in Render environment

# ── File list cache (avoids repeated GitHub API calls) ──
_details_cache      = []          # cached list of filenames
_details_cache_time = 0.0         # epoch timestamp of last fetch
_CACHE_TTL_SECONDS  = 600         # refresh every 10 minutes

# ── ASTM F2787 Live Load Model constants ──
# Faithful port of the verified "AQUACELL Loading Model_AquaCell_v1.xlsx" workbook
# (AquaCell Model - Truck / AquaCell Model - Outrigger / Data & Calculations sheets).
# Read cell-by-cell (formulas, not just values) on 2026-07-02. Two things this
# fixes vs. the old hardcoded version:
#   1. IM (Dynamic Allowance Factor) is NOT a fixed 24.75% — it's a formula
#      that depends on cover depth: IM(%) = 33*(1-0.125*depth_ft) for depth_ft
#      <= 8 ft, else 0%. 24.75% was only ever correct at exactly 24 in cover.
#   2. The projected-area spread and the "LLl" term now match the workbook's
#      three-tier depth model (< 18 in / 18 in-to-overlap / > overlap) with
#      per-truck-type transverse (wo) / longitudinal (lo) spread dimensions,
#      instead of a single symmetric (tire + cover*LLDF) approximation.
_LL_m    = 1.2      # Multiple Presence Factor, ASTM F2787 Section A1.2.1 (fixed)
_LL_LLDF = 1.15      # Live Load Distribution Factor, granular fill, Section A1.3.1 (fixed)
_LL_gLL  = 1.75      # Live load minimum FoS, Section A1.3 (fixed)
_LL_gDL  = 1.95      # Dead load minimum FoS, Section 7.3 (fixed)
_LL_FILL_PCF_DEFAULT = 120.0   # workbook's default unit weight of fill above AquaCell


def _ll_dynamic_allowance_pct(cover_in):
    """IM (%), Section A1.2.2 — depth-dependent, capped at 0 beyond 8 ft cover."""
    cover_ft = cover_in / 12.0
    if cover_ft > 8.0:
        return 0.0
    return 33.0 * (1.0 - 0.125 * cover_ft)


def _ll_wo_single_tire(cover_in):
    """Transverse (wo) spread, single-tire wheel — H-10."""
    if cover_in <= 18.0:
        return cover_in + 10.0
    elif cover_in <= 62.0:
        return _LL_LLDF * cover_in + 10.0
    else:
        return _LL_LLDF * (cover_in + 10.0 - (cover_in - 62.0) / 2.0)


def _ll_wo_dual_tire(cover_in):
    """Transverse (wo) spread, dual-tire wheel — HS-20 / HS-25."""
    if cover_in <= 18.0:
        return cover_in + 20.0
    elif cover_in <= 52.0:
        return _LL_LLDF * cover_in + 20.0
    else:
        return _LL_LLDF * (cover_in + 20.0 - (cover_in - 52.0) / 2.0)


def _ll_lo_tandem(cover_in):
    """Longitudinal (lo) spread, tandem-axle configuration."""
    if cover_in <= 18.0:
        return cover_in + 10.0
    elif cover_in <= 38.0:
        return _LL_LLDF * cover_in + 10.0
    else:
        return _LL_LLDF * (cover_in + 10.0 - (cover_in - 38.0) / 2.0)


def _ll_lo_not_tandem(cover_in):
    """Longitudinal (lo) spread, single/non-tandem axle configuration (no overlap cap in source)."""
    if cover_in <= 18.0:
        return cover_in + 10.0
    else:
        return _LL_LLDF * cover_in + 10.0


# ══════════════════════════════════════════════════════════════════
#  MIN / MAX COVER TABLE — Wavin AQ-100-08 Rev 2 (4/1/2026),
#  "AquaCell Cover & Burial Depths - Standard & EX Models"
#  Read directly from the drawing, not OCR'd from a corrupted scan.
#  Max cover figures are explicitly stated on the drawing as applying
#  to a SINGLE-LAYER tank; for multi-layer tanks the drawing states the
#  window of application "may be limited" without giving an exact
#  number, and directs you to contact Wavin directly.
# ══════════════════════════════════════════════════════════════════
_MIN_MAX_COVER_TABLE = {
    'SC': {
        'H10':  {'min_in': 12, 'max_ft': 14.4},
        'HS20': {'min_in': 18, 'max_ft': 14.4},
        'HS25': {'min_in': 30, 'max_ft': 14.1},
    },
    'EX': {
        'H10':  {'min_in': 12, 'max_ft': 26.2},
        'HS20': {'min_in': 16, 'max_ft': 26.2},
        'HS25': {'min_in': 22, 'max_ft': 26.1},
    },
}
# Used only by calculators that have no traffic-load context of their own
# (Outrigger) — the most conservative (smallest) single-layer max cover
# across all three AASHTO ratings for that configuration, since there's
# no way to know which traffic rating actually governs for those loads.
_CONSERVATIVE_MAX_COVER_FT = {
    'SC': min(v['max_ft'] for v in _MIN_MAX_COVER_TABLE['SC'].values()),
    'EX': min(v['max_ft'] for v in _MIN_MAX_COVER_TABLE['EX'].values()),
}


def calc_astm_f2787_truck(traffic_load, cover_depth_ft, config, fill_pcf=_LL_FILL_PCF_DEFAULT):
    """
    Full ASTM F2787 Truck Load Model — faithful port of the verified workbook.
    Returns a detail dict with every intermediate value shown in the workbook,
    or None if cover_depth_ft is invalid.
    """
    if cover_depth_ft is None or cover_depth_ft <= 0:
        return None

    cover_in = cover_depth_ft * 12.0
    im_pct   = _ll_dynamic_allowance_pct(cover_in)

    if traffic_load == 'H10':
        axle_lbs    = 16000.0
        tire_dims   = '10" x 10"'
        tire_area   = 100.0
        axle_config = 'Single Axle'
        wo_spread   = _ll_wo_single_tire(cover_in)
        lo_spread   = _ll_lo_not_tandem(cover_in)
    elif traffic_load == 'HS25':
        axle_lbs    = 40000.0
        tire_dims   = '10" x 20"'
        tire_area   = 200.0
        axle_config = 'Single Axle'
        wo_spread   = _ll_wo_dual_tire(cover_in)
        lo_spread   = _ll_lo_not_tandem(cover_in)
    else:  # HS20 (default) — switches to a tandem-axle model above 38 in of cover
        traffic_load = 'HS20'
        tandem       = cover_in > 38.0
        axle_lbs     = 25000.0 if tandem else 32000.0
        tire_dims    = '10" x 20"'
        tire_area    = 200.0
        axle_config  = 'Tandem Axle' if tandem else 'Single Axle'
        wo_spread    = _ll_wo_dual_tire(cover_in)
        lo_spread    = _ll_lo_tandem(cover_in) if tandem else _ll_lo_not_tandem(cover_in)

    wheel_lbs = axle_lbs / 2.0
    proj_area = wo_spread * lo_spread

    ll_local_lbs    = 64.0 * tire_area / 144.0
    ll_trans_lbs    = wheel_lbs * _LL_m * (1.0 + im_pct / 100.0)
    factored_ll_lbs = ll_local_lbs + ll_trans_lbs
    ll_psi = (factored_ll_lbs / proj_area) if proj_area > 0 else None

    dl_psi       = (fill_pcf / 1728.0) * cover_in
    max_str      = 70.0 if config == 'SC' else 100.0
    # Verified per-traffic-load max cover (AQ-100-08 Rev 2) — NOT a flat
    # 20/30 ft by config. This is the single-layer figure; see the
    # dashboard's Min Cover / Burial Depth calculator for the multi-layer
    # caveat, which this simpler truck-load calculator doesn't carry.
    max_cover_ft = _MIN_MAX_COVER_TABLE[config][traffic_load]['max_ft']

    total_press = (ll_psi or 0.0) + dl_psi
    fos_live = round(max_str / total_press, 2) if total_press > 0 else None
    fos_dead = round(max_str / dl_psi, 2) if dl_psi > 0 else None

    return {
        'traffic_load':     traffic_load,
        'axle_lbs':         round(axle_lbs, 1),
        'axle_config':      axle_config,
        'wheel_lbs':        round(wheel_lbs, 1),
        'tire_dims':        tire_dims,
        'tire_area_in2':    round(tire_area, 1),
        'cover_in':         round(cover_in, 2),
        'cover_ft':         round(cover_depth_ft, 3),
        'fill_pcf':         fill_pcf,
        'im_pct':           round(im_pct, 3),
        'wo_spread_in':     round(wo_spread, 3),
        'lo_spread_in':     round(lo_spread, 3),
        'proj_area_in2':    round(proj_area, 2),
        'll_local_lbs':     round(ll_local_lbs, 2),
        'll_trans_lbs':     round(ll_trans_lbs, 2),
        'factored_ll_lbs':  round(factored_ll_lbs, 2),
        'll_psi':           round(ll_psi, 3) if ll_psi is not None else None,
        'dl_psi':           round(dl_psi, 3),
        'config':           config,
        'max_strength_psi': max_str,
        'max_cover_ft':     max_cover_ft,
        'fos_live':         fos_live,
        'fos_dead':         fos_dead,
        'min_fos_live':     _LL_gLL,
        'min_fos_dead':     _LL_gDL,
        'status_live':      ('PASS' if (fos_live is not None and fos_live >= _LL_gLL) else ('FAIL' if fos_live is not None else 'N/A')),
        'status_dead':      ('PASS' if (fos_dead is not None and fos_dead >= _LL_gDL) else ('FAIL' if fos_dead is not None else 'N/A')),
    }


def calc_live_load_fos(traffic_load, cover_depth_ft, config):
    """
    Thin wrapper preserving the original 4-tuple return shape
    (ll_psi, dl_psi, fos_live_load, fos_dead_load) used by the existing
    single-tank and multi-tank routes. All logic now lives in
    calc_astm_f2787_truck(); this just unpacks it.
    """
    d = calc_astm_f2787_truck(traffic_load, cover_depth_ft, config)
    if d is None:
        return None, None, None, None
    return d['ll_psi'], d['dl_psi'], d['fos_live'], d['fos_dead']


def calc_outrigger_load(total_weight_lbs, pad_shape, pad_length_in, pad_width_in, pad_diameter_in,
                         cover_depth_ft, fill_pcf, load_factor_pct, config):
    """
    AquaCell Outrigger Load Model — faithful port of the verified workbook
    (AquaCell Model - Outrigger / Data & Calculations sheets).

    Note: the source workbook computes a Safety Factor for the outrigger
    load but does NOT state a minimum required value anywhere on any sheet
    (unlike the Truck model's explicit gammaLL=1.75 / gammaDL=1.95). This
    function therefore returns the calculated FoS with no PASS/FAIL judgment
    baked in — that threshold should come from you, not be invented here.
    """
    if cover_depth_ft is None or cover_depth_ft <= 0:
        return {'error': 'Enter a valid cover depth.'}
    if total_weight_lbs is None or total_weight_lbs <= 0:
        return {'error': 'Enter a valid total vehicle/equipment weight.'}

    cover_in = cover_depth_ft * 12.0

    if pad_shape == 'Circular':
        if not pad_diameter_in or pad_diameter_in <= 0:
            return {'error': 'Enter a valid outrigger pad diameter.'}
        contact_area = math.pi * (pad_diameter_in ** 2) / 4.0
        proj_area    = math.pi * ((pad_diameter_in + cover_in) ** 2) / 4.0
    else:
        pad_shape = 'Rectangular'
        if not pad_length_in or not pad_width_in or pad_length_in <= 0 or pad_width_in <= 0:
            return {'error': 'Enter valid outrigger pad length and width.'}
        contact_area = pad_length_in * pad_width_in
        proj_area    = (pad_length_in + cover_in) * (pad_width_in + cover_in)

    factored_psi = (total_weight_lbs * (load_factor_pct / 100.0) / proj_area) if proj_area > 0 else None
    dl_psi       = (fill_pcf / 1728.0) * cover_in
    max_str      = 70.0 if config == 'SC' else 100.0
    # Outrigger loads don't map to an AASHTO traffic rating (H10/HS20/HS25),
    # so there's no single verified max-cover figure for this load type in
    # AQ-100-08. Using the most conservative (smallest) single-layer max
    # cover across all three ratings for this config, clearly flagged as
    # such in the results/PDF disclaimer.
    max_cover_ft = _CONSERVATIVE_MAX_COVER_FT[config]

    total_press = (factored_psi or 0.0) + dl_psi
    fos = round(max_str / total_press, 2) if total_press > 0 else None

    return {
        'pad_shape':        pad_shape,
        'contact_area_in2': round(contact_area, 2),
        'proj_area_in2':    round(proj_area, 2),
        'cover_in':         round(cover_in, 2),
        'cover_ft':         round(cover_depth_ft, 3),
        'fill_pcf':         fill_pcf,
        'load_factor_pct':  load_factor_pct,
        'factored_psi':     round(factored_psi, 3) if factored_psi is not None else None,
        'dl_psi':           round(dl_psi, 3),
        'config':           config,
        'max_strength_psi': max_str,
        'max_cover_ft':     max_cover_ft,
        'fos':              fos,
        'min_fos':          None,
    }


# ══════════════════════════════════════════════════════════════════
#  DESIGN VERIFICATION DASHBOARD — Excavation Slope Calculator
#
#  Soil-type maximum allowable slopes are from OSHA 29 CFR 1926 Subpart P,
#  Appendix B (Sloping and Benching), confirmed directly against osha.gov
#  on 2026-07-02:
#    Type A        = 3/4 : 1  (53°)
#    Type A, short-term (<=24 hr, <=12 ft depth) = 1/2 : 1  (63°)
#    Type B        = 1 : 1    (45°)
#    Type C        = 1.5 : 1  (34°)
#    Stable Rock   = vertical (90°, no slope required)
#  These tabulated slopes apply only up to 20 ft of depth — beyond that,
#  OSHA requires a registered professional engineer's design (App. B is
#  not applicable). Soil classification itself (assigning Type A/B/C) must
#  be performed by a "competent person" per OSHA Appendix A visual/manual
#  tests — this tool does not classify soil, it only applies the tabulated
#  slope once a type is selected.
#
#  Excavation volume uses the prismoidal formula V = (H/6)*(A1+4*Am+A2).
#  This is NOT an approximation for this specific shape: since the
#  excavation's cross-sectional area at any depth z is
#      Area(z) = (L + 2*z*ratio) * (W + 2*z*ratio)
#  — a degree-2 polynomial in z — and the prismoidal formula is exact for
#  any polynomial up to degree 3, the two are mathematically identical
#  here. This was verified by direct integration, not just recalled as a
#  general civil-engineering rule of thumb.
# ══════════════════════════════════════════════════════════════════
_OSHA_SLOPE_RATIOS = {
    'TypeA':            0.75,
    'TypeA_ShortTerm':  0.5,
    'TypeB':            1.0,
    'TypeC':            1.5,
    'StableRock':       0.0,
}
_OSHA_SLOPE_LABELS = {
    'TypeA':            'Type A (\u00be:1, 53\u00b0)',
    'TypeA_ShortTerm':  'Type A \u2014 Short-Term \u226424hr, \u226412ft (\u00bd:1, 63\u00b0)',
    'TypeB':            'Type B (1:1, 45\u00b0)',
    'TypeC':            'Type C (1\u00bd:1, 34\u00b0)',
    'StableRock':       'Stable Rock (vertical, 90\u00b0)',
    'Custom':           'Custom (user-entered ratio)',
}


def calc_excavation_slope(payload):
    config    = payload.get('config', 'SC')
    width_ft  = float(payload.get('width_ft', 0) or 0)
    length_ft = float(payload.get('length_ft', 0) or 0)
    layers    = int(float(payload.get('layers', 1) or 1))
    cover_ft  = float(payload.get('cover_ft', 0) or 0)
    soil_type = payload.get('soil_type', 'TypeB')
    ratio     = float(payload.get('slope_ratio', 1.0) or 0)

    if width_ft <= 0 or length_ft <= 0:
        return {'error': 'Enter a valid tank footprint (width & length).'}
    if ratio < 0:
        return {'error': 'Slope ratio cannot be negative.'}

    cd = CONFIG_DATA.get(config, CONFIG_DATA['SC'])
    layer_heights = cd['layer_heights']
    layers = max(1, min(layers, len(layer_heights)))
    tank_height_ft = layer_heights[layers - 1]

    total_depth_ft = cover_ft + tank_height_ft
    if total_depth_ft <= 0:
        return {'error': 'Enter a valid cover depth.'}

    offset_per_side_ft = total_depth_ft * ratio
    slope_angle_deg = math.degrees(math.atan(1.0 / ratio)) if ratio > 0 else 90.0

    bottom_length_ft = length_ft
    bottom_width_ft  = width_ft
    top_length_ft    = length_ft + 2.0 * offset_per_side_ft
    top_width_ft     = width_ft + 2.0 * offset_per_side_ft
    mid_length_ft     = length_ft + offset_per_side_ft
    mid_width_ft      = width_ft + offset_per_side_ft

    a_bottom = bottom_length_ft * bottom_width_ft
    a_top    = top_length_ft * top_width_ft
    a_mid    = mid_length_ft * mid_width_ft

    volume_cf = (total_depth_ft / 6.0) * (a_bottom + 4.0 * a_mid + a_top)
    volume_cy = volume_cf / 27.0

    warnings = []
    if total_depth_ft > 20.0:
        warnings.append('Excavation depth exceeds 20 ft \u2014 OSHA tabulated slopes (Appendix B) only apply '
                         'up to 20 ft. Excavations deeper than 20 ft require a registered professional '
                         'engineer\u2019s design, not a tabulated slope.')
    if total_depth_ft >= 5.0:
        warnings.append('Excavation is 5 ft or deeper \u2014 OSHA 1926.652 requires a protective system '
                         '(sloping, benching, shoring, or shielding) unless made entirely in stable rock.')
    if soil_type == 'TypeC' and total_depth_ft > 0:
        warnings.append('Benching is not permitted in Type C soil per OSHA Appendix B \u2014 sloping or '
                         'another protective system must be used.')

    return {
        'config':               config,
        'tank_height_ft':       round(tank_height_ft, 3),
        'cover_ft':             round(cover_ft, 2),
        'total_depth_ft':       round(total_depth_ft, 3),
        'soil_type':            soil_type,
        'soil_type_label':      _OSHA_SLOPE_LABELS.get(soil_type, 'Custom (user-entered ratio)'),
        'slope_ratio':          round(ratio, 3),
        'slope_angle_deg':      round(slope_angle_deg, 1),
        'offset_per_side_ft':   round(offset_per_side_ft, 3),
        'bottom_length_ft':     round(bottom_length_ft, 2),
        'bottom_width_ft':      round(bottom_width_ft, 2),
        'top_length_ft':        round(top_length_ft, 2),
        'top_width_ft':         round(top_width_ft, 2),
        'a_bottom_sf':          round(a_bottom, 1),
        'a_mid_sf':             round(a_mid, 1),
        'a_top_sf':             round(a_top, 1),
        'volume_cf':            round(volume_cf, 1),
        'volume_cy':            round(volume_cy, 2),
        'warnings':             warnings,
    }


# ══════════════════════════════════════════════════════════════════
#  DESIGN VERIFICATION DASHBOARD — Stepped Perimeter Backfill Calculator
#
#  This is a FIRST-PRINCIPLES geometric derivation, not a Wavin-verified
#  source. It replaces the source workbook's simplified two-box-difference
#  method (outer footprint x depth minus structure footprint x depth,
#  scaled by an unexplained "40% net" factor) with an exact calculation
#  of the actual staircase-shaped excavated void.
#
#  Model: starting at the structure, the excavation steps outward N times
#  on the way to grade. Each step = one sloped riser (height = step height,
#  running out horizontally at the given slope ratio) topped by a flat
#  bench (width = bench width) -- except the topmost riser, which reaches
#  grade directly with NO trailing bench (a bench sitting above ground
#  level doesn't physically exist). N = ceil(total_depth / step_height);
#  if that doesn't divide evenly, the topmost riser absorbs the remainder
#  (shorter than the others), per your call on rounding.
#
#  Within each riser, the excess cross-sectional area (excavation minus
#  structure) is a degree-2 polynomial in depth -- exactly like the plain
#  Excavation Slope calculator -- so the prismoidal formula
#  V = (h/6)*(A_bottom + 4*A_mid + A_top) is exact for that riser, not an
#  approximation. Summing all risers gives the exact total volume for
#  this geometry. Independently verified against brute-force numerical
#  integration (200,000 slices): matched to within 0.00003%.
#
#  OSHA note: benching is not permitted in Type C soil (29 CFR 1926 Subpart
#  P, Appendix B) -- confirmed directly against osha.gov. Type A/B soils
#  have specific tabulated maximum bench height/width limits in Table B-1;
#  secondary sources gave slightly inconsistent numbers for those specific
#  figures (e.g. 4 ft vs. 5 ft for "subsequent benches"), so this tool
#  does NOT hardcode or enforce a specific bench dimension limit -- it
#  only flags that Type C prohibits benching outright, and directs you to
#  verify your chosen step height/bench width against Table B-1 yourself
#  for Type A/B.
# ══════════════════════════════════════════════════════════════════
_MAX_BACKFILL_STEPS = 500  # sanity cap against a degenerate tiny step height


def _backfill_excess_area(offset_ft, length_ft, width_ft):
    """Excess plan area (excavation minus structure) at a given horizontal offset."""
    return (length_ft + 2.0 * offset_ft) * (width_ft + 2.0 * offset_ft) - length_ft * width_ft


def calc_stepped_backfill(payload):
    config      = payload.get('config', 'SC')
    width_ft    = float(payload.get('width_ft', 0) or 0)
    length_ft   = float(payload.get('length_ft', 0) or 0)
    layers      = int(float(payload.get('layers', 1) or 1))
    cover_ft    = float(payload.get('cover_ft', 0) or 0)
    step_h_ft   = float(payload.get('step_height_ft', 0) or 0)
    bench_w_ft  = float(payload.get('bench_width_ft', 0) or 0)
    soil_type   = payload.get('soil_type', 'TypeB')
    ratio       = float(payload.get('slope_ratio', 1.0) or 0)

    if width_ft <= 0 or length_ft <= 0:
        return {'error': 'Enter a valid tank footprint (width & length).'}
    if step_h_ft <= 0:
        return {'error': 'Enter a valid step height (> 0 ft).'}
    if bench_w_ft < 0 or ratio < 0:
        return {'error': 'Bench width and slope ratio cannot be negative.'}

    cd = CONFIG_DATA.get(config, CONFIG_DATA['SC'])
    layer_heights = cd['layer_heights']
    layers = max(1, min(layers, len(layer_heights)))
    tank_height_ft = layer_heights[layers - 1]

    total_depth_ft = cover_ft + tank_height_ft
    if total_depth_ft <= 0:
        return {'error': 'Enter a valid cover depth.'}

    n_steps = max(1, math.ceil(total_depth_ft / step_h_ft))
    if n_steps > _MAX_BACKFILL_STEPS:
        return {'error': f'Step height too small for this depth — would require {n_steps:,} steps '
                          f'(limit {_MAX_BACKFILL_STEPS}). Enter a larger step height.'}

    total_volume_cf = 0.0
    offset_before = 0.0
    step_rows = []
    for k in range(1, n_steps + 1):
        h_k = step_h_ft if k < n_steps else (total_depth_ft - (n_steps - 1) * step_h_ft)
        offset_top_of_riser = offset_before + ratio * h_k
        a_bot = _backfill_excess_area(offset_before, length_ft, width_ft)
        a_top = _backfill_excess_area(offset_top_of_riser, length_ft, width_ft)
        a_mid = _backfill_excess_area((offset_before + offset_top_of_riser) / 2.0, length_ft, width_ft)
        v_k = (h_k / 6.0) * (a_bot + 4.0 * a_mid + a_top)
        total_volume_cf += v_k
        step_rows.append({
            'step': k,
            'riser_height_ft': round(h_k, 3),
            'offset_start_ft': round(offset_before, 3),
            'offset_end_ft': round(offset_top_of_riser, 3),
            'volume_cf': round(v_k, 1),
        })
        offset_before = offset_top_of_riser + (bench_w_ft if k < n_steps else 0.0)

    total_offset_at_grade = offset_before
    outer_length_ft = length_ft + 2.0 * total_offset_at_grade
    outer_width_ft  = width_ft + 2.0 * total_offset_at_grade
    volume_cy = total_volume_cf / 27.0

    slope_angle_deg = math.degrees(math.atan(1.0 / ratio)) if ratio > 0 else 90.0

    warnings = []
    if total_depth_ft > 20.0:
        warnings.append('Excavation depth exceeds 20 ft — OSHA tabulated slopes/benching (Appendix B) only '
                         'apply up to 20 ft. A registered professional engineer must design excavations '
                         'deeper than 20 ft.')
    if total_depth_ft >= 5.0:
        warnings.append('Excavation is 5 ft or deeper — OSHA 1926.652 requires a protective system '
                         '(sloping, benching, shoring, or shielding) unless made entirely in stable rock.')
    if soil_type == 'TypeC':
        warnings.append('Benching is NOT permitted in Type C soil per OSHA Appendix B — use the '
                         'Excavation Slope (sloping-only) calculator instead for Type C.')
    warnings.append('OSHA Table B-1 sets specific maximum bench height/width limits for Type A and Type B '
                     'soil. This tool does not enforce those specific tabulated dimensions — verify your '
                     'chosen step height and bench width against Table B-1 for your soil type.')

    return {
        'config':                config,
        'tank_height_ft':        round(tank_height_ft, 3),
        'cover_ft':              round(cover_ft, 2),
        'total_depth_ft':        round(total_depth_ft, 3),
        'soil_type':             soil_type,
        'soil_type_label':       _OSHA_SLOPE_LABELS.get(soil_type, 'Custom (user-entered ratio)'),
        'slope_ratio':           round(ratio, 3),
        'slope_angle_deg':       round(slope_angle_deg, 1),
        'step_height_ft':        round(step_h_ft, 3),
        'bench_width_ft':        round(bench_w_ft, 3),
        'n_steps':               n_steps,
        'bottom_length_ft':      round(length_ft, 2),
        'bottom_width_ft':       round(width_ft, 2),
        'total_offset_at_grade_ft': round(total_offset_at_grade, 3),
        'outer_length_ft':       round(outer_length_ft, 2),
        'outer_width_ft':        round(outer_width_ft, 2),
        'volume_cf':             round(total_volume_cf, 1),
        'volume_cy':             round(volume_cy, 2),
        'step_rows':             step_rows,
        'warnings':              warnings,
    }


# ══════════════════════════════════════════════════════════════════
#  DESIGN VERIFICATION DASHBOARD — Min Cover / Burial Depth Calculator
#
#  Uses the verified _MIN_MAX_COVER_TABLE (Wavin AQ-100-08, Rev 2,
#  4/1/2026) directly -- the same table already used to fix the Truck
#  and Outrigger calculators' max_cover_ft. This calculator's whole
#  purpose is to check a proposed cover depth against that table.
#
#  Min cover PASS/FAIL is layer-count-independent (the drawing gives
#  no such caveat for min cover -- it's a top-layer structural
#  concern). Max cover is explicitly a single-layer figure on the
#  drawing; for layers > 1 this returns a CAUTION status rather than
#  a clean PASS/FAIL, per your call.
# ══════════════════════════════════════════════════════════════════
def calc_min_cover_burial(payload):
    config      = payload.get('config', 'SC')
    width_ft    = float(payload.get('width_ft', 0) or 0)
    length_ft   = float(payload.get('length_ft', 0) or 0)
    layers      = int(float(payload.get('layers', 1) or 1))
    cover_ft    = float(payload.get('cover_ft', 0) or 0)
    traffic_load = payload.get('traffic_load', 'HS20')
    if traffic_load not in ('H10', 'HS20', 'HS25'):
        traffic_load = 'HS20'

    if width_ft <= 0 or length_ft <= 0:
        return {'error': 'Enter a valid tank footprint (width & length).'}
    if cover_ft <= 0:
        return {'error': 'Enter a valid cover depth.'}

    cd = CONFIG_DATA.get(config, CONFIG_DATA['SC'])
    layer_heights = cd['layer_heights']
    layers = max(1, min(layers, len(layer_heights)))
    tank_height_ft = layer_heights[layers - 1]

    table_entry = _MIN_MAX_COVER_TABLE[config][traffic_load]
    min_cover_in = table_entry['min_in']
    min_cover_ft = min_cover_in / 12.0
    max_cover_ft = table_entry['max_ft']

    min_status = 'PASS' if cover_ft >= min_cover_ft else 'FAIL'

    if layers == 1:
        max_status = 'PASS' if cover_ft <= max_cover_ft else 'FAIL'
        max_status_label = max_status
    else:
        # Wavin's max-cover figure is explicitly for a single-layer tank.
        # Still compute the comparison (useful reference), but never claim
        # a verified PASS/FAIL for a configuration Wavin didn't tabulate.
        max_status = 'PASS' if cover_ft <= max_cover_ft else 'FAIL'
        max_status_label = 'CAUTION \u2014 NOT WAVIN-VERIFIED FOR MULTI-LAYER'

    warnings = []
    if layers > 1:
        warnings.append(_MULTI_LAYER_MAX_COVER_NOTE)
    if min_status == 'FAIL':
        warnings.append(f'Proposed cover ({cover_ft} ft) is below the {min_cover_ft:.2f} ft '
                         f'({min_cover_in:.0f} in) minimum required for {traffic_load} traffic on '
                         f'{"SC" if config == "SC" else "EX"} \u2014 per Wavin AQ-100-08.')
    if max_status == 'FAIL':
        warnings.append(f'Proposed cover ({cover_ft} ft) exceeds the {max_cover_ft} ft maximum '
                         f'(single-layer) for {traffic_load} traffic on {"SC" if config == "SC" else "EX"} '
                         f'\u2014 per Wavin AQ-100-08.')

    return {
        'config':            config,
        'traffic_load':      traffic_load,
        'tank_height_ft':    round(tank_height_ft, 3),
        'layers':            layers,
        'cover_ft':          round(cover_ft, 3),
        'total_depth_ft':    round(cover_ft + tank_height_ft, 3),
        'min_cover_in':      round(min_cover_in, 1),
        'min_cover_ft':      round(min_cover_ft, 3),
        'max_cover_ft':      round(max_cover_ft, 2),
        'min_status':        min_status,
        'max_status':        max_status,
        'max_status_label':  max_status_label,
        'warnings':          warnings,
    }


_MULTI_LAYER_MAX_COVER_NOTE = (
    'Max cover shown is Wavin\u2019s single-layer figure (AQ-100-08, Rev 2). This tank has more than '
    '1 layer, and Wavin states the window of application "may be limited" for multi-layer tanks '
    'without giving an exact reduced number \u2014 consult Wavin directly before relying on this max '
    'cover figure for a multi-layer design.'
)


# ══════════════════════════════════════════════════════════════════
#  DESIGN VERIFICATION DASHBOARD — Complex Shape Builder (Groundwork)
#
#  Per ROADMAP.md v17: "CRATE BUILDER FOR COMPLEX (ENTER ROW BY ROW,
#  THEN DESIGNATE HOW MANY CRATES IN THE ROW)". This is phase one:
#  the data model and derived-values calc only. No drawing yet.
#
#  A complex (non-rectangular) footprint is built as an ordered list
#  of rows. Each row is one crate-length deep and some whole number
#  of crates wide, indented by some whole number of crates from a
#  shared reference edge. Stacking rows with varying width/offset
#  produces any rectilinear shape (L, T, U, staircase, notch) without
#  needing a separate "subtract" concept -- that's what makes this
#  simpler than a composite-rectangle model. Discrete obstructions
#  (light poles, manhole islands) stay the job of the EXISTING void
#  space entry feature above/below this function, layered on top of
#  whatever outer shape the row builder produces.
#
#  crate_count and offset_crates are both integers -- there's no such
#  thing as a fractional crate; every row's crates must align to the
#  same underlying module grid to actually interlock in the field.
# ══════════════════════════════════════════════════════════════════
_MAX_SHAPE_ROWS = 200  # sanity cap


# ── Envelope → rows converter ──────────────────────────────────────
#  "Fit to footprint" mode. Given a scaled width and length (typically
#  read off a civil plan in Bluebeam), generate a rectangular whole-
#  crate row model that fits INSIDE that footprint (snaps DOWN — never
#  exceeds the entered envelope). The row model produced flows through
#  the exact same verified perimeter/area/drawing math as hand-built
#  rows, so nothing downstream changes.
#
#  IMPORTANT GEOMETRY FACT: MODULE_LEN (3.937) == 2 * MODULE_WID
#  (1.9685) exactly. The AquaCell module is a clean 2:1 rectangle,
#  so both crate orientations tile the same MODULE_WID grid with no
#  fractional columns.
#
#  The downstream row model is fixed at MODULE_LEN-deep rows counted in
#  MODULE_WID columns. The FOOTPRINT RECTANGLE and PERIMETER are
#  identical regardless of crate orientation — only the internal crate
#  TALLY differs. So envelope mode reports BOTH orientation counts and
#  flags which one is active; the drawing's internal grid is schematic
#  (MODULE_LEN strips) and labeled as such.
#
#    orientation 'len_along_length'  (crate long axis runs down the
#        tank length): crate = MODULE_LEN deep x MODULE_WID wide, so
#        every grid cell is one crate → crates = n_rows * n_cols.
#    orientation 'len_along_width'   (crate long axis runs across the
#        tank width): crate = MODULE_WID deep x MODULE_LEN wide →
#        crates = (length // MODULE_WID) * (width // MODULE_LEN).
# ───────────────────────────────────────────────────────────────────
def _envelope_to_rows(width_ft, length_ft):
    n_rows = int(length_ft // MODULE_LEN)   # MODULE_LEN-deep strips down the length
    n_cols = int(width_ft // MODULE_WID)    # MODULE_WID columns across the width
    if n_rows < 1 or n_cols < 1:
        return None

    rows = [{'crate_count': n_cols, 'offset_crates': 0} for _ in range(n_rows)]

    crates_len_along_length = n_rows * n_cols
    rows_b = int(length_ft // MODULE_WID)
    cols_b = int(width_ft // MODULE_LEN)
    crates_len_along_width = rows_b * cols_b

    return {
        'rows': rows,
        'n_rows': n_rows,
        'n_cols': n_cols,
        'crates_len_along_length': crates_len_along_length,
        'crates_len_along_width':  crates_len_along_width,
        'used_width_ft':  round(n_cols * MODULE_WID, 3),
        'used_length_ft': round(n_rows * MODULE_LEN, 3),
    }


# ══════════════════════════════════════════════════════════════════
#  DESIGN VERIFICATION DASHBOARD — Min Distance from Structure
#  (Building / Footing / Retaining-Wall Setback)
#
#  Purpose: where an adjacent foundation, footing, or retaining wall
#  bears DEEPER than a shallow structure but the AquaCell tank base
#  sits deeper still, lateral load from that structure can project
#  down-and-out along a soil failure/influence wedge and bear on the
#  crates. This tool returns the minimum horizontal setback that keeps
#  the tank clear of that wedge.
#
#  Method (ported verbatim from James's internal Wavin
#  "Distance from building" calculator spreadsheet — there is NO
#  standalone Wavin drawing number for this; attribute to the
#  internal method only):
#
#      L_min = (H2 - H1) * tan( (90 - a) deg )
#
#  where
#      H1 = depth of building/footing foundation (ft, below grade)
#      H2 = depth of tank base incl. bedding      (ft, below grade)
#      a  = soil internal shear / friction angle  (deg, from horizontal)
#
#  Equivalent form: L_min = (H2 - H1) / tan(a). The wedge rises from
#  the tank base at angle 'a' above horizontal; the horizontal run
#  over the vertical difference (H2 - H1) is the required setback.
#
#  Edge case (H2 <= H1): the tank base is at or above the foundation
#  base, so the wedge never reaches the tank — L_min = 0 ft, with a
#  note that no setback is required on this basis.
# ══════════════════════════════════════════════════════════════════
def calc_min_distance_structure(payload):
    def _num(key, default):
        # Preserve an explicit 0 (do NOT collapse via `or default`); only fall
        # back to default when the field is genuinely absent/blank/None.
        v = payload.get(key, default)
        if v in (None, ''):
            v = default
        return float(v)

    try:
        h1 = _num('h1_found_depth_ft', 0)   # foundation depth
        h2 = _num('h2_tank_depth_ft', 0)    # tank base depth
        angle = _num('shear_angle_deg', 45) # internal shear angle
    except (TypeError, ValueError):
        return {'error': 'Enter valid numeric values for all fields.'}

    proposed = payload.get('proposed_distance_ft', None)
    try:
        proposed = float(proposed) if proposed not in (None, '', 0, '0') else None
    except (TypeError, ValueError):
        proposed = None

    if h1 < 0 or h2 < 0:
        return {'error': 'Depths must be zero or positive (measured below finish grade).'}
    if angle <= 0 or angle >= 90:
        return {'error': 'Internal shear angle must be between 0 and 90 degrees (exclusive).'}

    depth_diff = h2 - h1
    notes = []

    if depth_diff <= 0:
        # Tank base is at or above the foundation base — wedge cannot reach it.
        l_min = 0.0
        wedge_clears = True
        notes.append(
            'Tank base is at or above the adjacent foundation base (H2 \u2264 H1). The soil '
            'load-influence wedge cannot reach the tank, so no horizontal setback is required '
            'on this basis. Confirm site-specific soils and surcharge conditions with the '
            'Engineer of Record.'
        )
    else:
        wedge_clears = False
        l_min = depth_diff * math.tan(math.radians(90.0 - angle))

    status = None
    margin_ft = None
    if proposed is not None:
        margin_ft = round(proposed - l_min, 3)
        status = 'PASS' if proposed >= l_min - 1e-9 else 'FAIL'
        if status == 'FAIL':
            notes.append(
                f'Proposed horizontal distance ({proposed:.2f} ft) is LESS than the required '
                f'minimum setback ({l_min:.2f} ft). The tank falls within the structure\u2019s '
                f'load-influence wedge \u2014 increase the setback, deepen/underpin the structure, '
                f'or provide a designed shoring / load-transfer solution reviewed by the EoR.'
            )

    return {
        'h1_found_depth_ft':   round(h1, 3),
        'h2_tank_depth_ft':    round(h2, 3),
        'shear_angle_deg':     round(angle, 2),
        'depth_diff_ft':       round(depth_diff, 3),
        'l_min_ft':            round(l_min, 3),
        'l_min_in':            round(l_min * 12.0, 1),
        'wedge_clears':        wedge_clears,
        'proposed_distance_ft': (round(proposed, 3) if proposed is not None else None),
        'margin_ft':           margin_ft,
        'status':              status,
        'notes':               notes,
    }


def calc_complex_shape_builder(payload):
    config = payload.get('config', 'SC')
    layers = int(float(payload.get('layers', 1) or 1))
    mode = payload.get('mode', 'rows')

    # ── Envelope mode: synthesize the row model from a footprint ────
    envelope_meta = None
    if mode == 'envelope':
        try:
            env_w = float(payload.get('envelope_width_ft', 0) or 0)
            env_l = float(payload.get('envelope_length_ft', 0) or 0)
        except (TypeError, ValueError):
            return {'error': 'Envelope width and length must be numbers.'}
        if env_w <= 0 or env_l <= 0:
            return {'error': 'Enter a positive width and length for the footprint.'}

        env = _envelope_to_rows(env_w, env_l)
        if env is None:
            return {'error': 'Footprint is too small to fit even one crate module.'}

        orientation = payload.get('orientation', 'len_along_length')
        if orientation not in ('len_along_length', 'len_along_width'):
            orientation = 'len_along_length'

        rows_in = env['rows']
        envelope_meta = {
            'entered_width_ft':  round(env_w, 3),
            'entered_length_ft': round(env_l, 3),
            'used_width_ft':     env['used_width_ft'],
            'used_length_ft':    env['used_length_ft'],
            'orientation':       orientation,
            'crates_len_along_length': env['crates_len_along_length'],
            'crates_len_along_width':  env['crates_len_along_width'],
        }
    else:
        rows_in = payload.get('rows', [])

    if not isinstance(rows_in, list) or len(rows_in) == 0:
        return {'error': 'Add at least one row to build the shape.'}
    if len(rows_in) > _MAX_SHAPE_ROWS:
        return {'error': f'Too many rows (limit {_MAX_SHAPE_ROWS}).'}

    cd = CONFIG_DATA.get(config, CONFIG_DATA['SC'])
    layer_heights = cd['layer_heights']
    layers = max(1, min(layers, len(layer_heights)))
    tank_height_ft = layer_heights[layers - 1]

    rows = []
    total_crates_layer = 0
    max_extent_crates = 0
    for i, row_in in enumerate(rows_in):
        try:
            crate_count = int(float(row_in.get('crate_count', 0) or 0))
            offset_crates = int(float(row_in.get('offset_crates', 0) or 0))
        except (TypeError, ValueError, AttributeError):
            return {'error': f'Row {i + 1}: crate count and offset must be whole numbers.'}

        if crate_count <= 0:
            return {'error': f'Row {i + 1}: crate count must be at least 1.'}
        if offset_crates < 0:
            return {'error': f'Row {i + 1}: offset cannot be negative.'}

        total_crates_layer += crate_count
        max_extent_crates = max(max_extent_crates, offset_crates + crate_count)
        rows.append({
            'row_index':     i,
            'crate_count':   crate_count,
            'offset_crates': offset_crates,
            'row_width_ft':  round(crate_count * MODULE_WID, 3),
            'row_offset_ft': round(offset_crates * MODULE_WID, 3),
        })

    n_rows = len(rows)
    bounding_width_crates  = max_extent_crates
    bounding_length_crates = n_rows

    # ── Perimeter of the row-union footprint ────────────────────────
    # Each row is a single contiguous horizontal strip spanning crate
    # columns [offset, offset + crate_count). Perimeter = exposed edges
    # of the union of these strips.
    #   • Vertical edges (left/right of a row) run in the LENGTH
    #     direction, each MODULE_LEN long. Because every row is one
    #     contiguous span, each row always exposes exactly its left and
    #     right edge → 2 * MODULE_LEN per row.
    #   • Horizontal edges (top/bottom of a row) run in the WIDTH
    #     direction. A row's top/bottom edge is exposed only where it
    #     does NOT overlap the adjacent row above/below.
    # Verified against brute-force per-crate edge rasterization
    # (rectangle, corner-notch L, and centered-notch traces all match).
    # ASSUMPTION: rows have no internal gaps (data model can't express
    # one). If gapped rows are ever added, the vertical term must be
    # recomputed from exposed spans.
    perim_vertical_ft   = 2.0 * n_rows * MODULE_LEN
    perim_horizontal_ft = 0.0
    for i, r in enumerate(rows):
        lo = r['offset_crates']
        hi = lo + r['crate_count']
        for nb in (i - 1, i + 1):
            if 0 <= nb < n_rows:
                nlo = rows[nb]['offset_crates']
                nhi = nlo + rows[nb]['crate_count']
                overlap = max(0, min(hi, nhi) - max(lo, nlo))
            else:
                overlap = 0
            exposed_cols = r['crate_count'] - overlap
            perim_horizontal_ft += exposed_cols * MODULE_WID

    perimeter_ft = perim_horizontal_ft + perim_vertical_ft

    net_footprint_area_sf     = total_crates_layer * MODULE_WID * MODULE_LEN
    bounding_width_ft         = bounding_width_crates * MODULE_WID
    bounding_length_ft        = bounding_length_crates * MODULE_LEN
    bounding_box_area_sf      = bounding_width_ft * bounding_length_ft
    fill_efficiency_pct       = (net_footprint_area_sf / bounding_box_area_sf * 100.0) if bounding_box_area_sf > 0 else 0.0

    # ── Envelope mode: the per-orientation physical crate tally can
    #    differ from the raw MODULE_WID-column count (grid cells).
    #    Override the reported crate count with the selected
    #    orientation's true tally. The footprint rectangle, perimeter,
    #    and drawing are unchanged — only the count reflects orientation.
    if envelope_meta is not None:
        if envelope_meta['orientation'] == 'len_along_width':
            total_crates_layer = envelope_meta['crates_len_along_width']
        else:
            total_crates_layer = envelope_meta['crates_len_along_length']
        net_footprint_area_sf = total_crates_layer * MODULE_WID * MODULE_LEN
        fill_efficiency_pct = (net_footprint_area_sf / bounding_box_area_sf * 100.0) if bounding_box_area_sf > 0 else 0.0

    total_crates_all_layers = total_crates_layer * layers

    result = {
        'config':                   config,
        'layers':                   layers,
        'mode':                     mode,
        'tank_height_ft':           round(tank_height_ft, 3),
        'rows':                     rows,
        'n_rows':                   n_rows,
        'total_crates_layer':       total_crates_layer,
        'total_crates_all_layers':  total_crates_all_layers,
        'bounding_width_crates':    bounding_width_crates,
        'bounding_length_crates':   bounding_length_crates,
        'bounding_width_ft':        round(bounding_width_ft, 3),
        'bounding_length_ft':       round(bounding_length_ft, 3),
        'net_footprint_area_sf':    round(net_footprint_area_sf, 2),
        'bounding_box_area_sf':     round(bounding_box_area_sf, 2),
        'fill_efficiency_pct':      round(fill_efficiency_pct, 1),
        'perimeter_ft':             round(perimeter_ft, 2),
        'perim_horizontal_ft':      round(perim_horizontal_ft, 2),
        'perim_vertical_ft':        round(perim_vertical_ft, 2),
        'module_wid_ft':            MODULE_WID,
        'module_len_ft':            MODULE_LEN,
    }
    if envelope_meta is not None:
        result['envelope'] = envelope_meta
    return result


# ══════════════════════════════════════════════════════════════════
#  VOID SPACE ENTRY  (Complex Shape mode)
#  Open areas inside the tank footprint (concrete islands, light
#  poles, monuments, etc.) that the tank must form around.
#    • Void dims snap UP to whole crate modules (must fully clear
#      the obstruction) — the main footprint snaps DOWN.
#    • Snapped void area is subtracted from the tank footprint.
#    • Void perimeter is added to the tank perimeter (interior
#      walls take side plates).
#    • The ENTIRE void footprint earns ZERO storage credit — no
#      ring, no core split, no separation fabric. The contractor
#      backfills the void with whatever they like (native, stone,
#      mix); it is reported as "Void Fill Required (by others)" =
#      total void area × tank height. Base/cover stone are unaffected
#      (they always run the full excavation footprint).
# ══════════════════════════════════════════════════════════════════

def parse_void_spaces(form, perimeter_stone_width, tank_height):
    MODULE_WID = 1.9685
    MODULE_LEN = 3.937
    enabled = form.get('void_enabled', '0') == '1'

    voids = []
    total_area = total_perim = 0.0
    total_crates_layer = 0

    if enabled:
        i = 0
        while f'void_area_{i}' in form:
            area_in  = float(form.get(f'void_area_{i}', 0) or 0)
            dim_in   = float(form.get(f'void_dim_{i}', 0) or 0)
            perim_in = float(form.get(f'void_perim_{i}', 0) or 0)
            label    = (form.get(f'void_label_{i}', '') or '').strip() or f'Void {i+1}'
            if area_in > 0 and dim_in > 0:
                other_dim = area_in / dim_in
                # Snap UP — a crate may not remain inside the obstruction.
                # 1e-9 epsilon prevents float error bumping exact multiples.
                crates_w  = math.ceil(dim_in    / MODULE_WID - 1e-9)
                crates_l  = math.ceil(other_dim / MODULE_LEN - 1e-9)
                snapped_w = crates_w * MODULE_WID
                snapped_l = crates_l * MODULE_LEN
                v_area    = snapped_w * snapped_l
                v_perim   = perim_in if perim_in > 0 else 2 * (snapped_w + snapped_l)
                voids.append({
                    'label':        label,
                    'area_in':      area_in,
                    'dim_in':       dim_in,
                    'perim_in':     perim_in,
                    'other_dim':    round(other_dim, 3),
                    'snapped_w':    round(snapped_w, 3),
                    'snapped_l':    round(snapped_l, 3),
                    'crates_w':     crates_w,
                    'crates_l':     crates_l,
                    'crates_layer': crates_w * crates_l,
                    'snapped_area': round(v_area, 2),
                    'perim_used':   round(v_perim, 2),
                })
                total_area         += v_area
                total_perim        += v_perim
                total_crates_layer += crates_w * crates_l
            i += 1

    active    = enabled and total_area > 0
    fill_vol  = total_area * tank_height   # full void column — no storage credit

    return {
        'enabled':            enabled,
        'active':             active,
        'voids':              voids,
        'total_area':         total_area,
        'total_perim':        total_perim,
        'total_crates_layer': total_crates_layer,
        'fill_vol':           fill_vol,
    }

_VOID_INERT = {
    'enabled': False, 'active': False, 'voids': [],
    'total_area': 0.0, 'total_perim': 0.0, 'total_crates_layer': 0, 'fill_vol': 0.0,
}


@app.route('/', methods=['GET', 'POST'])
def index():
    results = None
    form_data = {}

    if request.method == 'POST':
        project_name = request.form.get('project_name', '')
        project_num  = request.form.get('project_num', '')
        location     = request.form.get('location', '')
        client       = request.form.get('client', '')
        estimator    = request.form.get('estimator', '')
        estimator_email = request.form.get('estimator_email', '')
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
        contingency_units    = int(request.form.get('contingency_units', 0) or 0)
        contingency_overridden = request.form.get('contingency_overridden', '0')
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
            complex_gross_area = snapped_known * snapped_other

            # ── Void Space Entry ──────────────────────────────────
            vs = parse_void_spaces(request.form, perimeter_stone_width, tank_height)
            complex_tank_area = max(0.0, complex_gross_area - vs['total_area'])
            crates_per_layer  = max(0, crates_known * crates_other - vs['total_crates_layer'])
            effective_perim   = complex_tank_perim + (vs['total_perim'] if vs['active'] else 0.0)

            num_crates        = crates_per_layer * layers
            gross_tank_vol    = complex_tank_area * tank_height
            tank_storage      = gross_tank_vol * void_ratio
            tank_perim_calc   = effective_perim
            side_plates       = round(tank_perim_calc * (layers * side_multiplier) / 5.17)
            if config == 'SC':
                base_units    = num_crates
                bottom_plates = crates_per_layer
            else:
                base_units    = num_crates * 2
                bottom_plates = 0
            # Excavation is FULL footprint — voids are dug and re-filled,
            # so excavation area/perimeter never shrink for voids.
            if complex_excav_area <= 0:
                complex_excav_area = (snapped_known + 2*perimeter_stone_width) * (snapped_other + 2*perimeter_stone_width)
            if complex_excav_perim <= 0:
                complex_excav_perim = complex_tank_perim + 8*perimeter_stone_width
            total_excavation_vol  = complex_excav_area * total_system_depth
            stone_envelope_volume = total_excavation_vol - gross_tank_vol
            # Void core NEVER earns storage credit. When core fill = native
            # it is also excluded from the stone purchase volume.
            # Entire void footprint earns NO storage credit (fill by others).
            void_fill_vol      = vs['fill_vol'] if vs['active'] else 0.0
            stone_storage_env  = max(0.0, stone_envelope_volume - void_fill_vol)
            total_stone_storage   = stone_storage_env * stone_void
            total_storage         = tank_storage + total_stone_storage
            geoTank  = round((2*complex_tank_area + effective_perim*tank_height) * (1 + geoWaste/100.0), 1)
            geoStone = round((2*complex_excav_area + complex_excav_perim*total_system_depth) * (1 + geoWaste/100.0), 1)
            geoTotal = round(geoTank + geoStone, 1)
            used_perimeter = round(effective_perim, 2)
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
            complex_gross_area  = None
            vs                  = dict(_VOID_INERT)
            void_fill_vol       = 0.0

        stone_backfill_bulk_ft3  = round(stone_envelope_volume * 1.10, 1)
        stone_backfill_bulk_yd3  = round(stone_backfill_bulk_ft3 / 27, 2)
        stone_backfill_bulk_tons = round(stone_backfill_bulk_ft3 * 100 / 2000, 2)

        if shape_mode == 'complex':
            excav_area_for_layers = complex_excav_area
            tank_footprint        = complex_tank_area
        else:
            excav_area_for_layers = outer_width * outer_length
            tank_footprint        = tank_width * tank_length

        # Entire void footprint is excluded from the Perimeter stone layer —
        # voids get NO storage credit; the contractor fills them (by others).
        _void_area_layers = vs['total_area'] if vs['active'] else 0.0
        stone_top_gross      = round(excav_area_for_layers * cover_stone, 1)
        stone_top_net        = round(stone_top_gross * stone_void, 1)
        stone_perim_gross    = round((excav_area_for_layers - tank_footprint - _void_area_layers) * tank_height, 1)
        stone_perim_net      = round(stone_perim_gross * stone_void, 1)
        stone_base_gross     = round(excav_area_for_layers * base_stone, 1)
        stone_base_net       = round(stone_base_gross * stone_void, 1)
        # Void fill (by others) — full void column at tank height, no credit.
        void_fill_ft3 = round(void_fill_vol, 1) if vs['active'] else 0.0
        void_fill_yd3 = round(void_fill_ft3 / 27, 2)
        stone_layer_total_gross = round(stone_top_gross + stone_perim_gross + stone_base_gross, 1)
        stone_layer_total_net   = round(stone_top_net + stone_perim_net + stone_base_net, 1)

        # ── Geogrid default quantities ────────────────────────────────
        # Top/Cover: excavation area + 2 ft overlap each side, with waste
        # Bottom: tank footprint only, with waste
        # If the estimator already submitted overrides, use those; otherwise auto-calc.
        _geo_overlap = 2.0   # ft beyond excavation perimeter each side
        if shape_mode == 'complex':
            _geo_top_area = complex_excav_area   # complex mode uses polygon area; no simple W×L to extend
        else:
            _geo_top_area = (outer_width + 2*_geo_overlap) * (outer_length + 2*_geo_overlap)
        _geo_bottom_area = tank_footprint

        _geogrid_top_auto    = math.ceil(_geo_top_area    * (1 + geoWaste/100.0) / 9)
        _geogrid_bottom_auto = math.ceil(_geo_bottom_area * (1 + geoWaste/100.0) / 9)

        # Respect user overrides submitted with the form (blank = first load → use auto)
        _top_raw    = request.form.get('geogrid_top_yd2',    '').strip()
        _bottom_raw = request.form.get('geogrid_bottom_yd2', '').strip()
        geogrid_top_yd2    = int(_top_raw)    if _top_raw    != '' else _geogrid_top_auto
        geogrid_bottom_yd2 = int(_bottom_raw) if _bottom_raw != '' else _geogrid_bottom_auto

        # ── PT-ROW™ Pre-Treatment Row ─────────────────────────────────
        # Crate void volume per single-layer crate (matches selected config)
        ptrow_enabled      = request.form.get('ptrow_enabled', '0') == '1'
        ptrow_method       = request.form.get('ptrow_method', 'volume')  # 'volume' | 'flow'
        ptrow_crate_vol    = round(MODULE_WID * MODULE_LEN * layer_heights[0] * void_ratio, 4)
        ptrow_layer_ht     = layer_heights[0]
        ptrow_wrap_ext     = 1.5
        FLOW_COEFF         = 0.464   # CFS per crate per Wavin PT-ROW™ Sizing Guidance

        ptrow_areas      = []
        ptrow_flow_areas = []
        ptrow_total_crates = 0

        if ptrow_method == 'flow':
            # Flow-based: # crates = CEILING(Q ÷ 0.464)
            fi = 0
            while f'ptrow_flow_cfs_{fi}' in request.form:
                cfs_val = float(request.form.get(f'ptrow_flow_cfs_{fi}', 0) or 0)
                lbl_val = request.form.get(f'ptrow_flow_label_{fi}', f'Area {fi+1}').strip()
                nc = math.ceil(cfs_val / FLOW_COEFF) if FLOW_COEFF > 0 else 0
                ptrow_flow_areas.append({
                    'label':    lbl_val or f'Area {fi+1}',
                    'cfs':      cfs_val,
                    'n_crates': nc,
                })
                ptrow_total_crates += nc
                fi += 1
        else:
            # Volume-based: # crates = CEILING(WQV × pct% ÷ crate_vol)
            idx = 0
            while f'ptrow_wqv_{idx}' in request.form:
                wqv_val = float(request.form.get(f'ptrow_wqv_{idx}', 0) or 0)
                pct_val = float(request.form.get(f'ptrow_pct_{idx}', 10) or 10)
                lbl_val = request.form.get(f'ptrow_label_{idx}', f'Area {idx+1}').strip() or f'Area {idx+1}'
                pt_vol  = round(wqv_val * pct_val / 100.0, 2)
                nc      = math.ceil(pt_vol / ptrow_crate_vol) if ptrow_crate_vol > 0 else 0
                ptrow_areas.append({
                    'label': lbl_val, 'wqv': wqv_val, 'pct': pct_val,
                    'pt_vol': pt_vol, 'n_crates': nc,
                })
                ptrow_total_crates += nc
                idx += 1

        # Woven fabric — taco wrap: bottom + 2 long sides + 1 back end
        # Single sheet laid flat then wrapped:
        #   fabric_width  = crate_width  + 2×layer_ht + 2×wrap_ext  (bottom + up both sides + overlap to secure)
        #   fabric_length = row_length   + 2×wrap_ext                (length + back end overlap + front tuck)
        if ptrow_enabled and ptrow_total_crates > 0:
            _ptrow_row_len   = ptrow_total_crates * MODULE_LEN
            _ptrow_fab_w     = MODULE_WID + 2*ptrow_layer_ht + 2*ptrow_wrap_ext
            _ptrow_fab_l     = _ptrow_row_len + 2*ptrow_wrap_ext
            _ptrow_fab_ft2   = _ptrow_fab_w * _ptrow_fab_l * (1 + geoWaste/100.0)
            ptrow_woven_yd2  = math.ceil(_ptrow_fab_ft2 / 9)
            ptrow_woven_ft2  = round(_ptrow_fab_ft2, 1)
        else:
            ptrow_woven_yd2  = 0
            ptrow_woven_ft2  = 0.0
            ptrow_crate_vol  = round(ptrow_crate_vol, 4)

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
            'complex_gross_area':   round(complex_gross_area, 2)  if shape_mode == 'complex' else None,
            'void_enabled':         vs['enabled'],
            'void_active':          vs['active'],
            'void_spaces':          vs['voids'],
            'void_total_area':      round(vs['total_area'], 1),
            'void_total_perim':     round(vs['total_perim'], 1),
            'void_total_crates':    vs['total_crates_layer'],
            'void_fill_ft3':        void_fill_ft3,
            'void_fill_yd3':        void_fill_yd3,
            'used_perimeter': round(used_perimeter, 2),
            'base_units': base_units, 'side_plates': side_plates,
            'bottom_plates': bottom_plates, 'pipe_connectors': pipe_connectors,
            'top_adapters_12': top_adapters_12, 'top_adapters_16': top_adapters_16,
            'contingency_units':    contingency_units,
                'contingency_overridden': contingency_overridden,
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
            'geogrid_top_yd2': geogrid_top_yd2,
            'geogrid_bottom_yd2': geogrid_bottom_yd2,
            'ptrow_enabled':       ptrow_enabled,
            'ptrow_method':        ptrow_method,
            'ptrow_crate_vol':     round(ptrow_crate_vol, 4),
            'ptrow_layer_ht':      round(ptrow_layer_ht, 4),
            'ptrow_areas':         ptrow_areas,
            'ptrow_flow_areas':    ptrow_flow_areas,
            'ptrow_total_crates':  ptrow_total_crates,
            'ptrow_woven_yd2':     ptrow_woven_yd2,
            'ptrow_woven_ft2':     ptrow_woven_ft2,
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

# ── Multi-Tank shared constants ──
CONFIG_DATA = {
    'SC': {
        'layer_heights':   [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581],
        'void_ratio':      0.95486,
        'side_multiplier': 1.312336,
        'max_strength':    70,
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
_MT_PALLETS = { 'base':56, 'side':120, 'bottom':120, 'pipe':60, 'adapter12':20, 'adapter16':12 }
_MT_WEIGHTS = { 'base':25.18, 'side':4.91, 'bottom':7.86, 'pipe':2.70, 'adapter12':11.02, 'adapter16':22.00 }


# ======================================================================
#  DESIGN VERIFICATION DASHBOARD -- Buoyancy / Uplift Calculator
#
#  Built from first-principles geotechnical/structural buoyancy
#  checking. NO verified Wavin-specific source exists for this
#  calculation -- every assumption below was explicitly signed off
#  by James on 2026-07-02:
#    - Critical case = tank EMPTY of stored water while soil is
#      saturated up to the design high groundwater depth.
#    - Soil resisting weight above the tank: user-toggleable between
#      full/saturated unit weight and buoyant/effective unit weight
#      (saturated - 62.4 pcf) for the submerged portion of the soil
#      column. Dry (above-groundwater) soil always uses moist weight.
#    - Minimum required Factor of Safety = 1.25.
#    - Side-wall soil friction is excluded (conservative).
#    - Tank self-weight is a ROUGH APPROXIMATION: crate count (footprint
#      floor-snapped to module grid x layers) x Base Unit weight only.
#      Side/bottom/pipe/adapter weights are NOT included -- self-weight
#      is a minor contributor to resisting force, and this was accepted
#      as a v1 simplification rather than a full BOM port.
#    - Groundwater depth left blank on the frontend is sent as 0 ft
#      (most conservative -- groundwater at grade).
# ======================================================================
_WATER_UNIT_WEIGHT = 62.4   # lb/ft3, fresh water
_BUOYANCY_MIN_FOS   = 1.25

def calc_buoyancy(payload):
    config        = payload.get('config', 'SC')
    width_ft      = float(payload.get('width_ft', 0) or 0)
    length_ft     = float(payload.get('length_ft', 0) or 0)
    layers        = int(float(payload.get('layers', 1) or 1))
    cover_ft      = float(payload.get('cover_ft', 0) or 0)
    gw_depth_ft   = float(payload.get('gw_depth_ft', 0) or 0)
    moist_pcf     = float(payload.get('moist_pcf', 0) or 0)
    sat_pcf       = float(payload.get('sat_pcf', 0) or 0)
    use_buoyant   = bool(payload.get('use_buoyant_soil', False))
    surcharge_psf = float(payload.get('surcharge_psf', 0) or 0)

    cd = CONFIG_DATA.get(config, CONFIG_DATA['SC'])
    layer_heights = cd['layer_heights']
    layers = max(1, min(layers, len(layer_heights)))
    tank_height_ft = layer_heights[layers - 1]

    footprint_area = width_ft * length_ft
    if footprint_area <= 0 or tank_height_ft <= 0:
        return {'error': 'Enter a valid tank footprint (width & length) and layer count.'}
    if moist_pcf <= 0 or sat_pcf <= 0:
        return {'error': 'Enter both a soil moist unit weight and a soil saturated unit weight (pcf).'}

    top_of_tank_depth    = cover_ft
    bottom_of_tank_depth = cover_ft + tank_height_ft

    # -- Submerged height of tank --
    if gw_depth_ft >= bottom_of_tank_depth:
        submerged_height_ft = 0.0
    elif gw_depth_ft <= top_of_tank_depth:
        submerged_height_ft = tank_height_ft
    else:
        submerged_height_ft = bottom_of_tank_depth - gw_depth_ft

    v_submerged = footprint_area * submerged_height_ft
    f_up = _WATER_UNIT_WEIGHT * v_submerged

    # -- Soil column above tank: split dry vs. submerged --
    dry_soil_depth_ft       = max(0.0, min(gw_depth_ft, cover_ft))
    submerged_soil_depth_ft = max(0.0, cover_ft - dry_soil_depth_ft)

    soil_weight_used_pcf = (sat_pcf - _WATER_UNIT_WEIGHT) if use_buoyant else sat_pcf
    soil_weight_used_pcf = max(0.0, soil_weight_used_pcf)

    w_soil_dry       = footprint_area * dry_soil_depth_ft * moist_pcf
    w_soil_submerged = footprint_area * submerged_soil_depth_ft * soil_weight_used_pcf
    w_soil = w_soil_dry + w_soil_submerged

    # -- Tank self-weight (rough approximation -- Base Unit count only) --
    crates_wide = math.floor(width_ft  / MODULE_WID)
    crates_long = math.floor(length_ft / MODULE_LEN)
    crate_count = crates_wide * crates_long * layers
    w_tank = crate_count * _MT_WEIGHTS['base']

    # -- Surcharge (dead load only -- no live/vehicle load) --
    w_surcharge = surcharge_psf * footprint_area

    f_resist = w_soil + w_tank + w_surcharge

    if f_up <= 0:
        fos = None
        status = 'N/A - NOT SUBMERGED'
    else:
        fos = round(f_resist / f_up, 2)
        status = 'PASS' if fos >= _BUOYANCY_MIN_FOS else 'FAIL'

    return {
        'config':                   config,
        'footprint_area_sf':        round(footprint_area, 1),
        'crates_wide':              crates_wide,
        'crates_long':              crates_long,
        'tank_height_ft':           round(tank_height_ft, 3),
        'top_of_tank_depth_ft':     round(top_of_tank_depth, 2),
        'bottom_of_tank_depth_ft':  round(bottom_of_tank_depth, 2),
        'gw_depth_ft':              round(gw_depth_ft, 2),
        'submerged_height_ft':      round(submerged_height_ft, 3),
        'v_submerged_cf':           round(v_submerged, 1),
        'f_up_lbs':                 round(f_up, 1),
        'dry_soil_depth_ft':        round(dry_soil_depth_ft, 2),
        'submerged_soil_depth_ft':  round(submerged_soil_depth_ft, 2),
        'soil_weight_used_pcf':     round(soil_weight_used_pcf, 1),
        'use_buoyant_soil':         use_buoyant,
        'w_soil_lbs':               round(w_soil, 1),
        'crate_count_approx':       crate_count,
        'w_tank_lbs':               round(w_tank, 1),
        'w_surcharge_lbs':          round(w_surcharge, 1),
        'f_resist_lbs':             round(f_resist, 1),
        'fos':                      fos,
        'min_fos':                  _BUOYANCY_MIN_FOS,
        'status':                   status,
    }


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


def _draw_page1_header(c, logo_path, project_name, project_num, location, client, estimator, generated_str):
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

    # Project info box — 2-column grid (3 rows)
    info_h = 58
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

    _pf('Project Name',    project_name, col1_x, y - 4)
    _pf('Project Number',  project_num,  col2_x, y - 4)
    _pf('Location',        location,     col1_x, y - 24)
    _pf('Client',          client,       col2_x, y - 24)
    _pf('Estimator',       estimator,    col1_x, y - 44)

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


def _wrap_disclaimer_lines(text, font_sz=6.5):
    """Word-wrap disclaimer text to CW - 14pt, using pdfmetrics (no canvas needed)."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    max_w = CW - 14
    words = text.split()
    lines = []
    cur = ''
    for w in words:
        test = (cur + ' ' + w).strip()
        if stringWidth(test, 'Helvetica', font_sz) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _disclaimer_box_height(text, font_sz=6.5, line_h=9, pad=7):
    """Height the disclaimer box will occupy, without drawing anything —
    lets PDF builders reserve exactly the right amount of space above it."""
    return len(_wrap_disclaimer_lines(text, font_sz)) * line_h + pad * 2


def _draw_disclaimer_block(c, y_bottom, disclaimer_lines_raw=None, bold_triggers=None):
    """
    Render the full disclaimer box so its BOTTOM edge sits at y_bottom.
    Returns the y of the top of the box (useful if you need to know how much space it took).
    """
    if disclaimer_lines_raw is None:
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
    if bold_triggers is None:
        bold_triggers = ('FINAL LAYOUTS', 'LICENSED PROFESSIONAL', 'INSTALLATION DEPTHS')
    font_sz  = 6.5
    line_h   = 9
    pad      = 7

    lines   = _wrap_disclaimer_lines(disclaimer_lines_raw, font_sz)
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
        elif any(trig in line for trig in bold_triggers):
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
#  PDF BUILDER  —  Design Verification Dashboard: Buoyancy / Uplift
# ══════════════════════════════════════════════════════════════════
_BUOYANCY_DISCLAIMER_TEXT = (
    "DISCLAIMER: No verified Wavin source exists for this buoyancy/uplift check. This is a "
    "first-principles geotechnical/structural calculation (empty tank vs. saturated soil at the "
    "design high groundwater elevation) and is not a stamped engineering design. Soil moist and "
    "saturated unit weights shown on this sheet must come from the project's geotechnical report "
    "(soil borings / lab testing by the geotechnical engineer of record) — they are not defaulted "
    "or verified by this tool. Tank self-weight shown is a rough approximation (Base Unit count only) "
    "and does not reflect a full bill of materials. THE ENGINEER OF RECORD IS SOLELY RESPONSIBLE FOR "
    "VERIFYING ALL SOIL PARAMETERS, GROUNDWATER ELEVATIONS, AND THE FINAL BUOYANCY DESIGN using "
    "project-specific geotechnical data and applicable codes/standards."
)
_BUOYANCY_BOLD_TRIGGERS = ('THE ENGINEER OF RECORD', 'VERIFYING ALL SOIL')


def _draw_load_spread_diagram_pdf(c, x, y_top, w, h, top_width_in, cover_in, bottom_width_in, dim_label):
    """
    ReportLab canvas version of the web buildLoadSpreadSVG() diagram — a tire/pad
    contact footprint at grade spreading via 2V:1H distribution through cover
    soil to the wider projected footprint at the AquaCell. Drawn inside the
    box [x, y_top-h, x+w, y_top]. Illustrative, one dimension at a time.
    """
    top_margin = 22
    bottom_margin = 30
    usable_h = h - top_margin - bottom_margin
    grade_y = y_top - top_margin
    tank_y  = grade_y - usable_h

    max_half_w = max(top_width_in, bottom_width_in) / 2.0
    usable_half_w = (w / 2.0) - 12
    scale = usable_half_w / max(max_half_w, 1.0)

    cx = x + w / 2.0
    top_half_px = (top_width_in / 2.0) * scale
    bot_half_px = (bottom_width_in / 2.0) * scale

    # soil column
    c.setFillColor(colors.HexColor('#d6c9a8'))
    c.rect(x, tank_y, w, grade_y - tank_y, fill=1, stroke=0)

    # spread cone (dashed)
    c.setStrokeColor(colors.HexColor('#334155'))
    c.setLineWidth(0.75)
    c.setDash([3, 2])
    c.line(cx - top_half_px, grade_y, cx - bot_half_px, tank_y)
    c.line(cx + top_half_px, grade_y, cx + bot_half_px, tank_y)
    c.setDash([])

    # contact footprint bar at grade
    c.setFillColor(NAVY)
    c.rect(cx - top_half_px, grade_y, top_half_px * 2, 4, fill=1, stroke=0)

    # load arrow (red) down into footprint
    c.setStrokeColor(RED)
    c.setLineWidth(1.25)
    c.line(cx, grade_y + 15, cx, grade_y + 6)
    c.setFillColor(RED)
    p = c.beginPath()
    p.moveTo(cx - 3, grade_y + 6)
    p.lineTo(cx + 3, grade_y + 6)
    p.lineTo(cx, grade_y + 1)
    p.close()
    c.drawPath(p, fill=1, stroke=0)

    # AquaCell band at tank-top depth
    c.setFillColor(colors.HexColor('#cbd5e1'))
    c.setStrokeColor(NAVY)
    c.setLineWidth(0.75)
    c.rect(cx - bot_half_px, tank_y - 8, bot_half_px * 2, 8, fill=1, stroke=1)

    # pressure arrows (green) into the band
    c.setFillColor(GREEN)
    c.setStrokeColor(GREEN)
    c.setLineWidth(1.0)
    for frac in (0.3, 0.5, 0.7):
        ax = cx - bot_half_px + bot_half_px * 2 * frac
        c.line(ax, tank_y + 9, ax, tank_y + 1)
        p2 = c.beginPath()
        p2.moveTo(ax - 2, tank_y + 1)
        p2.lineTo(ax + 2, tank_y + 1)
        p2.lineTo(ax, tank_y - 2)
        p2.close()
        c.drawPath(p2, fill=1, stroke=0)

    # grade line + label
    c.setStrokeColor(colors.HexColor('#1e293b'))
    c.setLineWidth(1.0)
    c.line(x, grade_y, x + w, grade_y)
    c.setFillColor(colors.HexColor('#1e293b'))
    c.setFont('Helvetica-Bold', 5.5)
    c.drawString(x + 2, grade_y + 2, 'GRADE')

    # dimension labels
    c.setFillColor(NAVY)
    c.setFont('Helvetica-Bold', 6.5)
    c.drawCentredString(cx, grade_y + 20, f'{top_width_in:.1f} in')
    c.drawCentredString(cx, tank_y - 18, f'{bottom_width_in:.1f} in')
    c.setFillColor(GRAY)
    c.setFont('Helvetica', 5.5)
    c.drawCentredString(cx, tank_y - 27, f'{dim_label} spread at AquaCell')
    c.setFont('Helvetica', 5.5)
    c.drawRightString(x + w - 2, (grade_y + tank_y) / 2 + 4, 'Cover')
    c.drawRightString(x + w - 2, (grade_y + tank_y) / 2 - 4, f'{cover_in / 12.0:.2f} ft')


def _draw_buoyancy_diagram_pdf(c, x, y_top, w, h, r):
    """ReportLab canvas version of the web buoyancy cross-section diagram."""
    top_margin = 20
    bottom_margin = 8
    usable_h = h - top_margin - bottom_margin

    top_tank_depth = r['top_of_tank_depth_ft']
    bot_tank_depth = r['bottom_of_tank_depth_ft']
    gw_depth       = r['gw_depth_ft']

    max_ref = max(bot_tank_depth, gw_depth, 1)
    total_depth_shown = max_ref * 1.2
    px_per_ft = usable_h / total_depth_shown

    def y_at(depth_ft):
        return (y_top - top_margin) - depth_ft * px_per_ft

    box_left  = x + w * 0.16
    box_right = x + w * 0.84
    box_w     = box_right - box_left

    grade_y    = y_at(0)
    top_tank_y = y_at(top_tank_depth)
    bot_tank_y = y_at(bot_tank_depth)
    gw_shown   = min(gw_depth, total_depth_shown)
    gw_y       = y_at(gw_shown)

    soil_dry_bottom_depth = min(gw_depth, top_tank_depth)
    soil_dry_bottom_y = y_at(soil_dry_bottom_depth)
    has_soil_submerged = gw_depth < top_tank_depth

    # dry soil band
    c.setFillColor(colors.HexColor('#d6c9a8'))
    c.rect(box_left, soil_dry_bottom_y, box_w, grade_y - soil_dry_bottom_y, fill=1, stroke=0)

    # submerged soil band
    if has_soil_submerged:
        c.setFillColor(colors.HexColor('#a8967a'))
        c.rect(box_left, top_tank_y, box_w, soil_dry_bottom_y - top_tank_y, fill=1, stroke=0)

    # tank rect + illustrative crate grid
    c.setFillColor(colors.HexColor('#cbd5e1'))
    c.setStrokeColor(NAVY)
    c.setLineWidth(0.85)
    c.rect(box_left, bot_tank_y, box_w, top_tank_y - bot_tank_y, fill=1, stroke=1)
    c.setStrokeColor(colors.HexColor('#94a3b8'))
    c.setLineWidth(0.35)
    for i in range(1, 4):
        gx = box_left + (box_w / 4.0) * i
        c.line(gx, bot_tank_y, gx, top_tank_y)

    # submerged tank tint
    if r.get('submerged_height_ft', 0) > 0:
        sub_top_depth = max(gw_depth, top_tank_depth)
        sub_top_y = y_at(sub_top_depth)
        c.setFillColor(colors.Color(0.23, 0.51, 0.96, alpha=0.28))
        c.rect(box_left, bot_tank_y, box_w, sub_top_y - bot_tank_y, fill=1, stroke=0)

    # groundwater dashed line
    if gw_depth <= total_depth_shown:
        c.setStrokeColor(colors.HexColor('#2563eb'))
        c.setLineWidth(0.85)
        c.setDash([3, 2])
        c.line(box_left - 6, gw_y, box_right + 6, gw_y)
        c.setDash([])
        c.setFillColor(colors.HexColor('#2563eb'))
        c.setFont('Helvetica', 5.5)
        c.drawRightString(box_left - 8, gw_y - 2, 'GW')

    # grade line + label
    c.setStrokeColor(colors.HexColor('#1e293b'))
    c.setLineWidth(1.0)
    c.line(box_left - 12, grade_y, box_right + 12, grade_y)
    c.setFillColor(colors.HexColor('#1e293b'))
    c.setFont('Helvetica-Bold', 5.5)
    c.drawRightString(box_left - 14, grade_y + 2, 'GRADE')

    # resisting force arrows (green, down into top of tank)
    c.setFillColor(GREEN)
    c.setStrokeColor(GREEN)
    c.setLineWidth(0.9)
    for frac in (0.25, 0.5, 0.75):
        ax = box_left + box_w * frac
        c.line(ax, top_tank_y + 12, ax, top_tank_y + 2)
        p = c.beginPath()
        p.moveTo(ax - 2, top_tank_y + 2)
        p.lineTo(ax + 2, top_tank_y + 2)
        p.lineTo(ax, top_tank_y - 1)
        p.close()
        c.drawPath(p, fill=1, stroke=0)

    # uplift arrows (blue, up into bottom of tank) if submerged
    if r.get('f_up_lbs', 0) > 0:
        blue = colors.HexColor('#2563eb')
        c.setFillColor(blue)
        c.setStrokeColor(blue)
        for frac in (0.25, 0.5, 0.75):
            ax = box_left + box_w * frac
            c.line(ax, bot_tank_y - 12, ax, bot_tank_y - 2)
            p2 = c.beginPath()
            p2.moveTo(ax - 2, bot_tank_y - 2)
            p2.lineTo(ax + 2, bot_tank_y - 2)
            p2.lineTo(ax, bot_tank_y + 1)
            p2.close()
            c.drawPath(p2, fill=1, stroke=0)



def build_buoyancy_pdf(inputs, results, project_name=None):
    """
    Single-page submittal-ready PDF summarizing a buoyancy/uplift calculation.
    inputs:  the raw payload dict sent to calc_buoyancy()
    results: the dict returned by calc_buoyancy()
    Returns a BytesIO buffer positioned at 0.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    # ── Header band ──
    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'Buoyancy / Uplift Check — Preliminary, Non-Stamped Calculation')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 10

    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    def draw_section(y_top, title, rows, ncols=2):
        bar_h = 16
        c.setFillColor(BLUE)
        c.rect(LM, y_top - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y_top - bar_h + 4, title)

        n = len(rows)
        nrows = math.ceil(n / ncols)
        row_h = 20
        body_h = nrows * row_h + 12
        box_top = y_top - bar_h
        box_bottom = box_top - body_h
        c.setFillColor(LGRAY)
        c.setStrokeColor(MGRAY)
        c.setLineWidth(0.5)
        c.rect(LM, box_bottom, CW, body_h, fill=1, stroke=1)

        col_w = CW / ncols
        for i, (lbl, val) in enumerate(rows):
            col = i % ncols
            row_idx = i // ncols
            x = LM + 10 + col * col_w
            ry = box_top - 14 - row_idx * row_h
            c.setFillColor(GRAY)
            c.setFont('Helvetica', 7)
            c.drawString(x, ry, str(lbl).upper())
            c.setFillColor(BLACK)
            c.setFont('Helvetica-Bold', 8.5)
            c.drawString(x, ry - 11, str(val))
        return box_bottom - 8

    config_label = 'SC — Standard Capacity' if results.get('config') == 'SC' else 'EX — Extra Strong'
    soil_note    = '(buoyant/effective)' if results.get('use_buoyant_soil') else '(full/saturated)'
    footprint_sf = results.get('footprint_area_sf') or 0
    surcharge_psf = (results.get('w_surcharge_lbs', 0) / footprint_sf) if footprint_sf else 0

    input_rows = [
        ('Configuration',                    config_label),
        ('Footprint (W × L)',                 f"{inputs.get('width_ft')} ft × {inputs.get('length_ft')} ft"),
        ('Layers',                            inputs.get('layers')),
        ('Tank Height',                       f"{results['tank_height_ft']} ft"),
        ('Cover Depth',                       f"{inputs.get('cover_ft')} ft"),
        ('Design High Groundwater Depth',     f"{results['gw_depth_ft']} ft below grade"),
        ('Soil Moist Unit Weight',            f"{inputs.get('moist_pcf')} pcf"),
        ('Soil Saturated Unit Weight',        f"{inputs.get('sat_pcf')} pcf {soil_note}"),
        ('Surcharge Dead Load',                f"{surcharge_psf:.1f} psf"),
    ]
    y = draw_section(y, 'DESIGN INPUTS', input_rows, ncols=2)

    uplift_rows = [
        ('Submerged Tank Height',   f"{results['submerged_height_ft']} ft"),
        ('Submerged Volume',        f"{results['v_submerged_cf']:,.1f} ft³"),
        ('Water Unit Weight',       '62.4 pcf'),
        ('Uplift Force (F_up)',     f"{results['f_up_lbs']:,.1f} lb"),
    ]
    y = draw_section(y, 'UPLIFT FORCE', uplift_rows, ncols=2)

    resist_rows = [
        ('Dry Soil Depth',                  f"{results['dry_soil_depth_ft']} ft"),
        ('Submerged Soil Depth',            f"{results['submerged_soil_depth_ft']} ft"),
        ('Soil Weight (W_soil)',            f"{results['w_soil_lbs']:,.1f} lb"),
        ('Approx. Crate Count',             f"{results['crate_count_approx']:,} ({results['crates_wide']} × {results['crates_long']}/layer)"),
        ('Tank Self-Weight (approx.)',      f"{results['w_tank_lbs']:,.1f} lb"),
        ('Surcharge Dead Load',             f"{results['w_surcharge_lbs']:,.1f} lb"),
        ('Total Resisting Force (F_resist)', f"{results['f_resist_lbs']:,.1f} lb"),
    ]
    y = draw_section(y, 'RESISTING FORCE', resist_rows, ncols=2)

    # ── FoS hero box ──
    fos    = results.get('fos')
    status = results.get('status', 'N/A')
    fos_display = f"{fos:.2f}" if fos is not None else '—'

    if status == 'PASS':
        hero_bg, hero_border, hero_txt = LTGRN, GREEN, GREEN
    elif status == 'FAIL':
        hero_bg, hero_border, hero_txt = colors.HexColor('#fee2e2'), RED, RED
    else:
        hero_bg, hero_border, hero_txt = LGRAY, GRAY, GRAY

    hero_h = 74
    hero_bottom = y - hero_h
    c.setFillColor(hero_bg)
    c.setStrokeColor(hero_border)
    c.setLineWidth(1)
    c.rect(LM, hero_bottom, CW, hero_h, fill=1, stroke=1)
    c.setFillColor(BLACK)
    c.setFont('Helvetica-Bold', 30)
    c.drawCentredString(PW / 2, hero_bottom + 40, fos_display)
    c.setFillColor(GRAY)
    c.setFont('Helvetica', 8)
    c.drawCentredString(PW / 2, hero_bottom + 26, f"Factor of Safety  (min. required: {results.get('min_fos')})")
    c.setFillColor(hero_txt)
    c.setFont('Helvetica-Bold', 12)
    c.drawCentredString(PW / 2, hero_bottom + 8, status)

    y = hero_bottom - 10

    # ── Schematic cross-section (sized to use exactly the space available
    #    above the disclaimer, computed from its real wrapped height so the
    #    two never collide regardless of how long the disclaimer text is) ──
    disc_h = _disclaimer_box_height(_BUOYANCY_DISCLAIMER_TEXT)
    disc_top = 40 + disc_h
    available = y - disc_top - 10
    if available >= 90:
        diagram_h = min(available - 16, 170)
        bar_h = 16
        c.setFillColor(BLUE)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'CROSS-SECTION (NOT TO SCALE)')
        diagram_top = y - bar_h
        diagram_bottom = diagram_top - diagram_h
        c.setFillColor(LGRAY)
        c.setStrokeColor(MGRAY)
        c.setLineWidth(0.5)
        c.rect(LM, diagram_bottom, CW, diagram_h, fill=1, stroke=1)
        diagram_w = 220
        diagram_x = LM + (CW - diagram_w) / 2.0
        _draw_buoyancy_diagram_pdf(c, diagram_x, diagram_top - 6, diagram_w, diagram_h - 10, results)
        y = diagram_bottom - 10

    # ── Disclaimer (anchored at bottom margin) ──
    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_BUOYANCY_DISCLAIMER_TEXT,
                            bold_triggers=_BUOYANCY_BOLD_TRIGGERS)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


# ══════════════════════════════════════════════════════════════════
#  PDF BUILDER  —  Design Verification Dashboard: Loading Model (Truck)
# ══════════════════════════════════════════════════════════════════
_LOADING_TRUCK_DISCLAIMER_TEXT = (
    "DISCLAIMER: This is a faithful port of Wavin's verified AquaCell Loading Model workbook "
    "(ASTM F2787 Truck Load Model), read cell-by-cell and cross-checked against the workbook's own "
    "example values. It is a preliminary design check, not a stamped engineering design. Assumptions "
    "carried over from the source workbook: only vertical pressure is considered (no side thrust or "
    "moments); creep is not considered; 6 ft between wheels on an axle and 4 ft between tandem axles "
    "(center to center); projected area is based on 2V:1H stress distribution with overlap areas "
    "distributed evenly. THE ENGINEER OF RECORD IS SOLELY RESPONSIBLE FOR VERIFYING ALL LOADING "
    "CONDITIONS, COVER DEPTHS, AND THE FINAL STRUCTURAL DESIGN using project-specific plans and "
    "applicable codes/standards."
)
_LOADING_BOLD_TRIGGERS = ('THE ENGINEER OF RECORD',)


def _draw_section_kv(c, y_top, title, rows, ncols=2):
    """Shared section-table drawer used by the loading-model PDFs (same visual
    language as build_buoyancy_pdf's inline draw_section, factored out here
    since two PDF builders need it)."""
    bar_h = 16
    c.setFillColor(BLUE)
    c.rect(LM, y_top - bar_h, CW, bar_h, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(LM + 8, y_top - bar_h + 4, title)

    n = len(rows)
    nrows = math.ceil(n / ncols)
    row_h = 20
    body_h = nrows * row_h + 12
    box_top = y_top - bar_h
    box_bottom = box_top - body_h
    c.setFillColor(LGRAY)
    c.setStrokeColor(MGRAY)
    c.setLineWidth(0.5)
    c.rect(LM, box_bottom, CW, body_h, fill=1, stroke=1)

    col_w = CW / ncols
    for i, (lbl, val) in enumerate(rows):
        col = i % ncols
        row_idx = i // ncols
        x = LM + 10 + col * col_w
        ry = box_top - 14 - row_idx * row_h
        c.setFillColor(GRAY)
        c.setFont('Helvetica', 7)
        c.drawString(x, ry, str(lbl).upper())
        c.setFillColor(BLACK)
        c.setFont('Helvetica-Bold', 8.5)
        c.drawString(x, ry - 11, str(val))
    return box_bottom - 8


def _draw_fos_hero(c, y_top, value_display, sub_label, status, hero_h=64):
    """Shared FoS hero box drawer used by the loading-model PDFs."""
    if status == 'PASS':
        bg, border, txt = LTGRN, GREEN, GREEN
    elif status == 'FAIL':
        bg, border, txt = colors.HexColor('#fee2e2'), RED, RED
    else:
        bg, border, txt = LGRAY, GRAY, GRAY

    hero_bottom = y_top - hero_h
    c.setFillColor(bg)
    c.setStrokeColor(border)
    c.setLineWidth(1)
    c.rect(LM, hero_bottom, CW, hero_h, fill=1, stroke=1)
    c.setFillColor(BLACK)
    c.setFont('Helvetica-Bold', 24)
    c.drawCentredString(PW / 2, hero_bottom + hero_h - 26, value_display)
    c.setFillColor(GRAY)
    c.setFont('Helvetica', 7.5)
    c.drawCentredString(PW / 2, hero_bottom + hero_h - 40, sub_label)
    c.setFillColor(txt)
    c.setFont('Helvetica-Bold', 11)
    c.drawCentredString(PW / 2, hero_bottom + 8, status)
    return hero_bottom - 8


def build_loading_truck_pdf(inputs, results, project_name=None):
    """Single-page submittal-ready PDF for the ASTM F2787 Truck Load Model."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, f"Loading Model — Truck ({results['traffic_load']}), ASTM F2787")
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 10
    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    design_rows = [
        ('Traffic Load',           results['traffic_load']),
        ('Axle Load',              f"{results['axle_lbs']:,.0f} lb ({results['axle_config']})"),
        ('Wheel Load',             f"{results['wheel_lbs']:,.0f} lb"),
        ('Tire Contact Dimensions', results['tire_dims']),
        ('Tire Contact Area',      f"{results['tire_area_in2']:,.1f} in²"),
        ('Configuration',          'SC — Standard' if results['config'] == 'SC' else 'EX — Extra Strong'),
        ('Cover Depth',            f"{results['cover_ft']} ft ({results['cover_in']} in)"),
        ('Unit Weight of Fill',    f"{results['fill_pcf']} pcf"),
    ]
    y = _draw_section_kv(c, y, 'DESIGN TRUCK', design_rows, ncols=2)

    live_rows = [
        ('Dynamic Allowance Factor, IM', f"{results['im_pct']}%"),
        ('Multiple Presence Factor, m',  '1.2'),
        ('Transverse Spread (wo)',       f"{results['wo_spread_in']} in"),
        ('Longitudinal Spread (lo)',     f"{results['lo_spread_in']} in"),
        ('Projected Area',               f"{results['proj_area_in2']:,.1f} in²"),
        ('LLl (local)',                  f"{results['ll_local_lbs']:,.2f} lb"),
        ('LLt (transverse)',             f"{results['ll_trans_lbs']:,.1f} lb"),
        ('Factored Live Load',           f"{results['factored_ll_lbs']:,.1f} lb"),
        ('Factored Live Load Pressure',  f"{results['ll_psi']} psi"),
        ('Dead Load Pressure',           f"{results['dl_psi']} psi"),
        ('Max. Compressive Strength',    f"{results['max_strength_psi']} psi"),
        ('Max. Cover Depth',             f"{results['max_cover_ft']} ft"),
    ]
    y = _draw_section_kv(c, y, 'LIVE LOAD CALCULATION', live_rows, ncols=2)

    fos_live_disp = f"{results['fos_live']:.2f}" if results['fos_live'] is not None else '—'
    fos_dead_disp = f"{results['fos_dead']:.2f}" if results['fos_dead'] is not None else '—'

    hero_w = (CW - 10) / 2.0
    y_before_heroes = y
    # Two side-by-side hero boxes: live (left), dead (right)
    hero_h = 64
    hero_bottom = y_before_heroes - hero_h

    for i, (label, val, status) in enumerate([
        (f"Live Load FoS (min {results['min_fos_live']})", fos_live_disp, results['status_live']),
        (f"Dead Load FoS (min {results['min_fos_dead']})", fos_dead_disp, results['status_dead']),
    ]):
        box_x = LM + i * (hero_w + 10)
        if status == 'PASS':
            bg, border, txt = LTGRN, GREEN, GREEN
        elif status == 'FAIL':
            bg, border, txt = colors.HexColor('#fee2e2'), RED, RED
        else:
            bg, border, txt = LGRAY, GRAY, GRAY
        c.setFillColor(bg)
        c.setStrokeColor(border)
        c.setLineWidth(1)
        c.rect(box_x, hero_bottom, hero_w, hero_h, fill=1, stroke=1)
        c.setFillColor(BLACK)
        c.setFont('Helvetica-Bold', 22)
        c.drawCentredString(box_x + hero_w / 2, hero_bottom + hero_h - 26, val)
        c.setFillColor(GRAY)
        c.setFont('Helvetica', 7)
        c.drawCentredString(box_x + hero_w / 2, hero_bottom + hero_h - 39, label)
        c.setFillColor(txt)
        c.setFont('Helvetica-Bold', 10)
        c.drawCentredString(box_x + hero_w / 2, hero_bottom + 8, status)

    y = hero_bottom - 10

    # ── Schematic load-spread cross-sections (transverse + longitudinal,
    #    side by side), sized to use exactly the space available above the
    #    disclaimer so the two never collide regardless of text length ──
    disc_h = _disclaimer_box_height(_LOADING_TRUCK_DISCLAIMER_TEXT)
    disc_top = 40 + disc_h
    available = y - disc_top - 10
    if available >= 90:
        diagram_h = min(available - 16, 150)
        bar_h = 16
        c.setFillColor(BLUE)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'LOAD SPREAD (NOT TO SCALE)')
        diagram_top = y - bar_h
        diagram_bottom = diagram_top - diagram_h
        c.setFillColor(LGRAY)
        c.setStrokeColor(MGRAY)
        c.setLineWidth(0.5)
        c.rect(LM, diagram_bottom, CW, diagram_h, fill=1, stroke=1)

        # tire_dims is like '10" x 20"' — first number is longitudinal
        # (direction of travel), second is transverse, per the source workbook.
        dims_parts = results['tire_dims'].replace('"', '').split('x')
        lo_top_in = float(dims_parts[0].strip())
        wo_top_in = float(dims_parts[1].strip()) if len(dims_parts) > 1 else lo_top_in

        half_w = (CW - 20) / 2.0
        _draw_load_spread_diagram_pdf(c, LM + 6, diagram_top - 6, half_w - 6, diagram_h - 10,
                                       wo_top_in, results['cover_in'], results['wo_spread_in'], 'Transverse (wo)')
        _draw_load_spread_diagram_pdf(c, LM + half_w + 14, diagram_top - 6, half_w - 6, diagram_h - 10,
                                       lo_top_in, results['cover_in'], results['lo_spread_in'], 'Longitudinal (lo)')
        y = diagram_bottom - 10

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_LOADING_TRUCK_DISCLAIMER_TEXT,
                            bold_triggers=_LOADING_BOLD_TRIGGERS)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


# ══════════════════════════════════════════════════════════════════
#  PDF BUILDER  —  Design Verification Dashboard: Loading Model (Outrigger)
# ══════════════════════════════════════════════════════════════════
_LOADING_OUTRIGGER_DISCLAIMER_TEXT = (
    "DISCLAIMER: This is a faithful port of Wavin's verified AquaCell Loading Model workbook "
    "(AquaCell Outrigger Load Model), read cell-by-cell and cross-checked against the workbook's own "
    "example values. It is a preliminary design check, not a stamped engineering design. The source "
    "workbook does NOT state a minimum required Factor of Safety for this outrigger check anywhere — "
    "unlike the Truck model's explicit 1.75/1.95 minimums — so the Factor of Safety on this sheet is "
    "shown as a calculated value only, with no PASS/FAIL judgment applied. Projected area is based on "
    "simple 2V:1H stress distribution; only vertical pressure is considered (no side thrust or moments); "
    "creep is not considered. THE ENGINEER OF RECORD IS SOLELY RESPONSIBLE FOR VERIFYING ALL LOADING "
    "CONDITIONS, COVER DEPTHS, THE APPLICABLE MINIMUM FACTOR OF SAFETY, AND THE FINAL STRUCTURAL DESIGN "
    "using project-specific plans and applicable codes/standards."
)
_LOADING_OUTRIGGER_BOLD_TRIGGERS = ('THE ENGINEER OF RECORD',)


def build_loading_outrigger_pdf(inputs, results, project_name=None):
    """Single-page submittal-ready PDF for the AquaCell Outrigger Load Model."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'Loading Model — Outrigger / Crane Pad')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 10
    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    pad_rows = [
        ('Pad Shape',              results['pad_shape']),
        ('Contact Area',           f"{results['contact_area_in2']:,.1f} in²"),
        ('Configuration',          'SC — Standard' if results['config'] == 'SC' else 'EX — Extra Strong'),
        ('Cover Depth',            f"{results['cover_ft']} ft ({results['cover_in']} in)"),
        ('Unit Weight of Fill',    f"{results['fill_pcf']} pcf"),
        ('Outrigger Load Factor',  f"{results['load_factor_pct']}%"),
    ]
    y = _draw_section_kv(c, y, 'OUTRIGGER PAD', pad_rows, ncols=2)

    press_rows = [
        ('Projected Area (spread at AquaCell)', f"{results['proj_area_in2']:,.1f} in²"),
        ('Factored Pressure from Outrigger',    f"{results['factored_psi']} psi"),
        ('Dead Load Pressure',                  f"{results['dl_psi']} psi"),
        ('Max. Compressive Strength',           f"{results['max_strength_psi']} psi"),
        ('Max. Cover Depth',                    f"{results['max_cover_ft']} ft"),
    ]
    y = _draw_section_kv(c, y, 'PRESSURE & SAFETY FACTOR', press_rows, ncols=2)

    fos_disp = f"{results['fos']:.2f}" if results['fos'] is not None else '—'
    y = _draw_fos_hero(c, y, fos_disp, 'Factor of Safety — no minimum specified in source workbook',
                        'INFORMATIONAL ONLY', hero_h=64)

    # ── Schematic load-spread cross-section(s) — two side-by-side (length +
    #    width) for a rectangular pad, one centered (diameter) for circular,
    #    since a circular pad spreads identically in every direction ──
    disc_h = _disclaimer_box_height(_LOADING_OUTRIGGER_DISCLAIMER_TEXT)
    disc_top = 40 + disc_h
    available = y - disc_top - 10
    if available >= 90:
        diagram_h = min(available - 16, 150)
        bar_h = 16
        c.setFillColor(BLUE)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'LOAD SPREAD (NOT TO SCALE)')
        diagram_top = y - bar_h
        diagram_bottom = diagram_top - diagram_h
        c.setFillColor(LGRAY)
        c.setStrokeColor(MGRAY)
        c.setLineWidth(0.5)
        c.rect(LM, diagram_bottom, CW, diagram_h, fill=1, stroke=1)

        cover_in = results['cover_in']
        if results['pad_shape'] == 'Circular':
            diam_in = float(inputs.get('pad_diameter_in', 0) or 0)
            diagram_w = 220
            diagram_x = LM + (CW - diagram_w) / 2.0
            _draw_load_spread_diagram_pdf(c, diagram_x, diagram_top - 6, diagram_w, diagram_h - 10,
                                           diam_in, cover_in, diam_in + cover_in, 'Diameter')
        else:
            length_in = float(inputs.get('pad_length_in', 0) or 0)
            width_in  = float(inputs.get('pad_width_in', 0) or 0)
            half_w = (CW - 20) / 2.0
            _draw_load_spread_diagram_pdf(c, LM + 6, diagram_top - 6, half_w - 6, diagram_h - 10,
                                           length_in, cover_in, length_in + cover_in, 'Length')
            _draw_load_spread_diagram_pdf(c, LM + half_w + 14, diagram_top - 6, half_w - 6, diagram_h - 10,
                                           width_in, cover_in, width_in + cover_in, 'Width')
        y = diagram_bottom - 10

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_LOADING_OUTRIGGER_DISCLAIMER_TEXT,
                            bold_triggers=_LOADING_OUTRIGGER_BOLD_TRIGGERS)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


def _draw_excavation_slope_diagram_pdf(c, x, y_top, w, h, r):
    """
    ReportLab canvas diagram for the excavation slope cross-section: wide
    open cut at grade tapering down via a real (solid-line) slope to the
    narrow AquaCell footprint at the bottom. Unlike the load-spread
    diagrams, these slope lines are the actual excavated ground surface,
    not an illustrative stress-distribution cone — drawn solid, no arrows.
    """
    top_margin = 24
    bottom_margin = 30
    usable_h = h - top_margin - bottom_margin
    grade_y = y_top - top_margin
    tank_y  = grade_y - usable_h

    top_w_in    = r['top_length_ft'] * 12.0
    bottom_w_in = r['bottom_length_ft'] * 12.0
    max_half_w = max(top_w_in, bottom_w_in) / 2.0
    usable_half_w = (w / 2.0) - 12
    scale = usable_half_w / max(max_half_w, 1.0)

    cx = x + w / 2.0
    top_half_px = (top_w_in / 2.0) * scale
    bot_half_px = (bottom_w_in / 2.0) * scale

    # soil column (open excavation)
    c.setFillColor(colors.HexColor('#d6c9a8'))
    p = c.beginPath()
    p.moveTo(cx - top_half_px, grade_y)
    p.lineTo(cx + top_half_px, grade_y)
    p.lineTo(cx + bot_half_px, tank_y)
    p.lineTo(cx - bot_half_px, tank_y)
    p.close()
    c.drawPath(p, fill=1, stroke=0)

    # actual slope faces (solid lines — this is the real cut, not illustrative)
    c.setStrokeColor(colors.HexColor('#334155'))
    c.setLineWidth(1.1)
    c.line(cx - top_half_px, grade_y, cx - bot_half_px, tank_y)
    c.line(cx + top_half_px, grade_y, cx + bot_half_px, tank_y)

    # AquaCell band at the bottom of the excavation
    c.setFillColor(colors.HexColor('#cbd5e1'))
    c.setStrokeColor(NAVY)
    c.setLineWidth(0.85)
    c.rect(cx - bot_half_px, tank_y, bot_half_px * 2, 9, fill=1, stroke=1)
    c.setStrokeColor(colors.HexColor('#94a3b8'))
    c.setLineWidth(0.35)
    for i in range(1, 4):
        gx = cx - bot_half_px + (bot_half_px * 2 / 4.0) * i
        c.line(gx, tank_y, gx, tank_y + 9)

    # grade line + label
    c.setStrokeColor(colors.HexColor('#1e293b'))
    c.setLineWidth(1.0)
    c.line(x, grade_y, x + w, grade_y)
    c.setFillColor(colors.HexColor('#1e293b'))
    c.setFont('Helvetica-Bold', 5.5)
    c.drawString(x + 2, grade_y + 2, 'GRADE')

    # dimension labels
    c.setFillColor(NAVY)
    c.setFont('Helvetica-Bold', 6.5)
    c.drawCentredString(cx, grade_y + 18, f"{r['top_length_ft']:.1f} ft")
    c.drawCentredString(cx, tank_y - 16, f"{r['bottom_length_ft']:.1f} ft (tank)")
    c.setFillColor(GRAY)
    c.setFont('Helvetica', 5.5)
    c.drawRightString(x + w - 2, (grade_y + tank_y) / 2 + 8, f"{r['slope_ratio']}:1")
    c.drawRightString(x + w - 2, (grade_y + tank_y) / 2 - 2, f"{r['slope_angle_deg']}\u00b0")
    c.drawRightString(x + w - 2, (grade_y + tank_y) / 2 - 12, f"Depth {r['total_depth_ft']:.2f} ft")


_EXCAVATION_SLOPE_DISCLAIMER_TEXT = (
    "DISCLAIMER: Maximum allowable slopes shown are from OSHA 29 CFR 1926 Subpart P, Appendix B "
    "(Sloping and Benching), confirmed directly against osha.gov. These tabulated slopes apply only up "
    "to 20 ft of excavation depth -- excavations deeper than 20 ft require a registered professional "
    "engineer's design. Soil classification (assigning Type A/B/C) must be performed by a competent "
    "person per OSHA Appendix A visual and manual tests -- this tool does not classify soil, it only "
    "applies the tabulated slope once a type is selected. Excavation volume uses the prismoidal formula, "
    "which is mathematically exact for this uniform-slope rectangular shape, not an approximation. THE "
    "ENGINEER OF RECORD AND THE JOBSITE COMPETENT PERSON ARE SOLELY RESPONSIBLE FOR SOIL "
    "CLASSIFICATION, PROTECTIVE SYSTEM SELECTION, AND COMPLIANCE WITH ALL APPLICABLE OSHA AND LOCAL "
    "REQUIREMENTS."
)
_EXCAVATION_SLOPE_BOLD_TRIGGERS = ('THE ENGINEER OF RECORD AND THE JOBSITE',)


def build_excavation_slope_pdf(inputs, results, project_name=None):
    """Single-page submittal-ready PDF for the Excavation Slope calculator."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'Excavation Slope — OSHA 29 CFR 1926 Subpart P, Appendix B')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 10
    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    geom_rows = [
        ('Configuration',            'SC — Standard' if results['config'] == 'SC' else 'EX — Extra Strong'),
        ('Tank Footprint (bottom)',  f"{results['bottom_length_ft']} ft x {results['bottom_width_ft']} ft"),
        ('Tank Height',              f"{results['tank_height_ft']} ft"),
        ('Cover Depth',              f"{results['cover_ft']} ft"),
        ('Total Excavation Depth',   f"{results['total_depth_ft']} ft"),
        ('Soil Type',                results['soil_type_label']),
        ('Slope Ratio (H:V)',        f"{results['slope_ratio']}:1"),
        ('Slope Angle',              f"{results['slope_angle_deg']}\u00b0"),
    ]
    y = _draw_section_kv(c, y, 'EXCAVATION GEOMETRY', geom_rows, ncols=2)

    result_rows = [
        ('Offset per Side',          f"{results['offset_per_side_ft']} ft"),
        ('Top Footprint (at grade)', f"{results['top_length_ft']} ft x {results['top_width_ft']} ft"),
        ('Bottom Area',              f"{results['a_bottom_sf']:,.1f} sf"),
        ('Mid-Depth Area',           f"{results['a_mid_sf']:,.1f} sf"),
        ('Top Area',                 f"{results['a_top_sf']:,.1f} sf"),
        ('Excavation Volume',        f"{results['volume_cf']:,.1f} cf ({results['volume_cy']:,.2f} cy)"),
    ]
    y = _draw_section_kv(c, y, 'RESULTS', result_rows, ncols=2)

    warnings = results.get('warnings') or []
    if warnings:
        bar_h = 16
        c.setFillColor(AMBER)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'OSHA FLAGS')
        warn_top = y - bar_h
        line_h = 11
        pad = 8
        from reportlab.pdfbase.pdfmetrics import stringWidth
        max_w = CW - pad * 2
        wrapped = []
        for w_text in warnings:
            words = w_text.split()
            cur = ''
            for wd in words:
                test = (cur + ' ' + wd).strip()
                if stringWidth(test, 'Helvetica', 7.5) <= max_w:
                    cur = test
                else:
                    if cur:
                        wrapped.append(cur)
                    cur = wd
            if cur:
                wrapped.append(cur)
        warn_h = len(wrapped) * line_h + pad * 2
        warn_bottom = warn_top - warn_h
        c.setFillColor(LTAMB)
        c.setStrokeColor(AMBER)
        c.setLineWidth(0.6)
        c.rect(LM, warn_bottom, CW, warn_h, fill=1, stroke=1)
        ty = warn_top - pad - 6
        c.setFont('Helvetica', 7.5)
        c.setFillColor(BLACK)
        for line in wrapped:
            c.drawString(LM + pad, ty, line)
            ty -= line_h
        y = warn_bottom - 10

    disc_h = _disclaimer_box_height(_EXCAVATION_SLOPE_DISCLAIMER_TEXT)
    disc_top = 40 + disc_h
    available = y - disc_top - 10
    if available >= 90:
        diagram_h = min(available - 16, 170)
        bar_h = 16
        c.setFillColor(BLUE)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'CROSS-SECTION (NOT TO SCALE)')
        diagram_top = y - bar_h
        diagram_bottom = diagram_top - diagram_h
        c.setFillColor(LGRAY)
        c.setStrokeColor(MGRAY)
        c.setLineWidth(0.5)
        c.rect(LM, diagram_bottom, CW, diagram_h, fill=1, stroke=1)
        diagram_w = 240
        diagram_x = LM + (CW - diagram_w) / 2.0
        _draw_excavation_slope_diagram_pdf(c, diagram_x, diagram_top - 6, diagram_w, diagram_h - 10, results)
        y = diagram_bottom - 10

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_EXCAVATION_SLOPE_DISCLAIMER_TEXT,
                            bold_triggers=_EXCAVATION_SLOPE_BOLD_TRIGGERS)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


def _draw_stepped_backfill_diagram_pdf(c, x, y_top, w, h, r):
    """
    ReportLab canvas diagram for the stepped backfill cross-section:
    an actual staircase profile (solid lines — the real cut, not a
    stress cone), widening step by step from the tank at the bottom
    to the outer footprint at grade.
    """
    top_margin = 24
    bottom_margin = 26
    usable_h = h - top_margin - bottom_margin
    grade_y = y_top - top_margin
    tank_y  = grade_y - usable_h

    total_depth = r['total_depth_ft']
    px_per_ft = usable_h / total_depth if total_depth > 0 else 1.0

    top_w_in    = r['outer_length_ft']
    bottom_w_in = r['bottom_length_ft']
    max_half = max(top_w_in, bottom_w_in) / 2.0
    usable_half_w = (w / 2.0) - 12
    scale = usable_half_w / max(max_half, 1.0)

    cx = x + w / 2.0

    # Build the staircase outline (right side only; left side is a mirror).
    # z is measured from the tank (0) up to grade (total_depth). ReportLab's
    # y-axis increases UPWARD (origin bottom-left), so as z increases toward
    # grade, y must increase toward grade_y — i.e. ADD, not subtract.
    z = 0.0
    pts_right = [(cx + (r['bottom_length_ft'] / 2.0) * scale, tank_y)]
    for idx, row in enumerate(r['step_rows']):
        h_k = row['riser_height_ft']
        o_end = row['offset_end_ft']
        z += h_k
        y_after = tank_y + z * px_per_ft
        x_after = cx + (r['bottom_length_ft'] / 2.0 + o_end) * scale
        pts_right.append((x_after, y_after))
        # vertical jump for the flat bench (skipped on the last/topmost riser,
        # which reaches grade directly with no bench above it)
        if idx < len(r['step_rows']) - 1:
            bench_w = r['bench_width_ft']
            x_bench_end = cx + (r['bottom_length_ft'] / 2.0 + o_end + bench_w) * scale
            pts_right.append((x_bench_end, y_after))

    # soil fill (polygon: right staircase up, across grade, left staircase down, close at tank)
    pts_left = [(2 * cx - px, py) for (px, py) in pts_right]
    poly_pts = pts_right + list(reversed(pts_left))

    c.setFillColor(colors.HexColor('#d6c9a8'))
    p = c.beginPath()
    p.moveTo(poly_pts[0][0], poly_pts[0][1])
    for px, py in poly_pts[1:]:
        p.lineTo(px, py)
    p.close()
    c.drawPath(p, fill=1, stroke=0)

    # staircase outline (solid — actual cut)
    c.setStrokeColor(colors.HexColor('#334155'))
    c.setLineWidth(1.0)
    for side_pts in (pts_right, pts_left):
        pth = c.beginPath()
        pth.moveTo(side_pts[0][0], side_pts[0][1])
        for px, py in side_pts[1:]:
            pth.lineTo(px, py)
        c.drawPath(pth, fill=0, stroke=1)

    # AquaCell band at the bottom
    bottom_half_px = (r['bottom_length_ft'] / 2.0) * scale
    c.setFillColor(colors.HexColor('#cbd5e1'))
    c.setStrokeColor(NAVY)
    c.setLineWidth(0.85)
    c.rect(cx - bottom_half_px, tank_y, bottom_half_px * 2, 8, fill=1, stroke=1)

    # grade line + label
    c.setStrokeColor(colors.HexColor('#1e293b'))
    c.setLineWidth(1.0)
    c.line(x, grade_y, x + w, grade_y)
    c.setFillColor(colors.HexColor('#1e293b'))
    c.setFont('Helvetica-Bold', 5.5)
    c.drawString(x + 2, grade_y + 2, 'GRADE')

    # dimension labels
    c.setFillColor(NAVY)
    c.setFont('Helvetica-Bold', 6.5)
    c.drawCentredString(cx, grade_y + 16, f"{r['outer_length_ft']:.1f} ft")
    c.drawCentredString(cx, tank_y - 16, f"{r['bottom_length_ft']:.1f} ft (tank)")
    c.setFillColor(GRAY)
    c.setFont('Helvetica', 5.5)
    c.drawRightString(x + w - 2, (grade_y + tank_y) / 2 + 6, f"{r['n_steps']} steps")
    c.drawRightString(x + w - 2, (grade_y + tank_y) / 2 - 4, f"Depth {r['total_depth_ft']:.2f} ft")


_STEPPED_BACKFILL_DISCLAIMER_TEXT = (
    "DISCLAIMER: This is a first-principles geometric calculation, not a Wavin-verified source. It "
    "replaces a simplified two-box-difference method with an exact calculation of the actual "
    "stepped/benched excavated void: each step is a sloped riser topped by a flat bench, with the "
    "topmost riser reaching grade directly (no bench above ground level). Volume uses the prismoidal "
    "formula per riser, which is mathematically exact for this shape, independently verified against "
    "brute-force numerical integration. Benching is NOT permitted in Type C soil per OSHA Appendix B. "
    "OSHA Table B-1 sets specific maximum bench height/width limits for Type A and Type B soil -- this "
    "tool does not enforce those specific tabulated dimensions; verify your chosen step height and bench "
    "width against Table B-1 yourself. THE ENGINEER OF RECORD AND THE JOBSITE COMPETENT PERSON ARE "
    "SOLELY RESPONSIBLE FOR SOIL CLASSIFICATION, PROTECTIVE SYSTEM SELECTION, AND COMPLIANCE WITH ALL "
    "APPLICABLE OSHA AND LOCAL REQUIREMENTS."
)
_STEPPED_BACKFILL_BOLD_TRIGGERS = ('THE ENGINEER OF RECORD AND THE JOBSITE',)


def build_stepped_backfill_pdf(inputs, results, project_name=None):
    """Single-page submittal-ready PDF for the Stepped Perimeter Backfill calculator."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'Stepped Perimeter Backfill \u2014 First-Principles Geometry')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 10
    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    geom_rows = [
        ('Configuration',            'SC — Standard' if results['config'] == 'SC' else 'EX — Extra Strong'),
        ('Tank Footprint (bottom)',  f"{results['bottom_length_ft']} ft x {results['bottom_width_ft']} ft"),
        ('Tank Height',              f"{results['tank_height_ft']} ft"),
        ('Cover Depth',              f"{results['cover_ft']} ft"),
        ('Total Excavation Depth',   f"{results['total_depth_ft']} ft"),
        ('Soil Type',                results['soil_type_label']),
        ('Slope Ratio (H:V)',        f"{results['slope_ratio']}:1"),
        ('Step Height / Bench Width', f"{results['step_height_ft']} ft / {results['bench_width_ft']} ft"),
    ]
    y = _draw_section_kv(c, y, 'BENCHING GEOMETRY', geom_rows, ncols=2)

    result_rows = [
        ('Number of Steps',            str(results['n_steps'])),
        ('Total Offset at Grade',      f"{results['total_offset_at_grade_ft']} ft"),
        ('Outer Footprint (at grade)', f"{results['outer_length_ft']} ft x {results['outer_width_ft']} ft"),
        ('Backfill Volume',            f"{results['volume_cf']:,.1f} cf ({results['volume_cy']:,.2f} cy)"),
    ]
    y = _draw_section_kv(c, y, 'RESULTS', result_rows, ncols=2)

    warnings = results.get('warnings') or []
    if warnings:
        bar_h = 16
        c.setFillColor(AMBER)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'OSHA FLAGS')
        warn_top = y - bar_h
        line_h = 11
        pad = 8
        from reportlab.pdfbase.pdfmetrics import stringWidth
        max_w = CW - pad * 2
        wrapped = []
        for w_text in warnings:
            words = w_text.split()
            cur = ''
            for wd in words:
                test = (cur + ' ' + wd).strip()
                if stringWidth(test, 'Helvetica', 7.5) <= max_w:
                    cur = test
                else:
                    if cur:
                        wrapped.append(cur)
                    cur = wd
            if cur:
                wrapped.append(cur)
        warn_h = len(wrapped) * line_h + pad * 2
        warn_bottom = warn_top - warn_h
        c.setFillColor(LTAMB)
        c.setStrokeColor(AMBER)
        c.setLineWidth(0.6)
        c.rect(LM, warn_bottom, CW, warn_h, fill=1, stroke=1)
        ty = warn_top - pad - 6
        c.setFont('Helvetica', 7.5)
        c.setFillColor(BLACK)
        for line in wrapped:
            c.drawString(LM + pad, ty, line)
            ty -= line_h
        y = warn_bottom - 10

    disc_h = _disclaimer_box_height(_STEPPED_BACKFILL_DISCLAIMER_TEXT)
    disc_top = 40 + disc_h
    available = y - disc_top - 10
    if available >= 90:
        diagram_h = min(available - 16, 170)
        bar_h = 16
        c.setFillColor(BLUE)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'CROSS-SECTION (NOT TO SCALE)')
        diagram_top = y - bar_h
        diagram_bottom = diagram_top - diagram_h
        c.setFillColor(LGRAY)
        c.setStrokeColor(MGRAY)
        c.setLineWidth(0.5)
        c.rect(LM, diagram_bottom, CW, diagram_h, fill=1, stroke=1)
        diagram_w = 240
        diagram_x = LM + (CW - diagram_w) / 2.0
        _draw_stepped_backfill_diagram_pdf(c, diagram_x, diagram_top - 6, diagram_w, diagram_h - 10, results)
        y = diagram_bottom - 10

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_STEPPED_BACKFILL_DISCLAIMER_TEXT,
                            bold_triggers=_STEPPED_BACKFILL_BOLD_TRIGGERS)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


def _draw_min_cover_gauge_pdf(c, x, y_top, w, h, r):
    """
    ReportLab canvas "compliance gauge" for min/max cover: a horizontal
    bar spanning [min required -> max allowed], with the proposed cover
    plotted as a marker. Deliberately NOT drawn to a linear physical
    scale (min and max can differ by 10x+), so the diagram stays legible
    regardless of how far apart the two figures are.
    """
    min_ft  = r['min_cover_ft']
    max_ft  = r['max_cover_ft']
    cover_ft = r['cover_ft']

    bar_y = y_top - h / 2.0
    bar_left = x + 20
    bar_right = x + w - 20
    bar_w = bar_right - bar_left

    span = max(max_ft - min_ft, 0.01)

    def pos_of(v):
        frac = (v - min_ft) / span
        frac = max(-0.15, min(1.15, frac))
        return bar_left + frac * bar_w

    # bar background (the "safe" window)
    c.setFillColor(LTGRN if r['min_status'] == 'PASS' and r['max_status'] == 'PASS' else colors.HexColor('#fef3c7'))
    c.setStrokeColor(MGRAY)
    c.setLineWidth(0.75)
    c.rect(bar_left, bar_y - 8, bar_w, 16, fill=1, stroke=1)

    # min / max end ticks + labels
    c.setStrokeColor(colors.HexColor('#334155'))
    c.setLineWidth(1.25)
    c.line(bar_left, bar_y - 14, bar_left, bar_y + 14)
    c.line(bar_right, bar_y - 14, bar_right, bar_y + 14)

    c.setFillColor(colors.HexColor('#334155'))
    c.setFont('Helvetica-Bold', 7)
    c.drawCentredString(bar_left, bar_y - 22, f"Min {min_ft:.2f} ft")
    label = f"Max {max_ft:.1f} ft"
    if r['layers'] > 1:
        label += " *"
    c.drawCentredString(bar_right, bar_y - 22, label)

    # proposed-cover marker
    mx = pos_of(cover_ft)
    marker_color = RED if (r['min_status'] == 'FAIL' or r['max_status'] == 'FAIL') else colors.HexColor('#1d4ed8')
    c.setFillColor(marker_color)
    c.setStrokeColor(marker_color)
    c.setLineWidth(2)
    c.line(mx, bar_y - 20, mx, bar_y + 20)
    tri = c.beginPath()
    tri.moveTo(mx - 5, bar_y + 20)
    tri.lineTo(mx + 5, bar_y + 20)
    tri.lineTo(mx, bar_y + 27)
    tri.close()
    c.drawPath(tri, fill=1, stroke=0)
    c.setFont('Helvetica-Bold', 8)
    c.drawCentredString(mx, bar_y + 34, f"Proposed: {cover_ft:.2f} ft")

    if r['layers'] > 1:
        c.setFillColor(GRAY)
        c.setFont('Helvetica', 6.5)
        c.drawCentredString(x + w / 2.0, y_top - h + 4, "* single-layer figure \u2014 not Wavin-verified for multi-layer tanks")


_MIN_COVER_BURIAL_DISCLAIMER_TEXT = (
    "DISCLAIMER: Minimum and maximum cover depths shown are from Wavin drawing AQ-100-08, Rev 2 "
    "(4/1/2026), \"AquaCell Cover & Burial Depths - Standard & EX Models,\" read directly from the "
    "drawing. Minimum cover applies regardless of layer count. Maximum cover is explicitly stated on "
    "the drawing as applying to a SINGLE-LAYER tank; for multi-layer tanks Wavin states the window of "
    "application may be limited without giving an exact reduced figure. THE ENGINEER OF RECORD IS "
    "SOLELY RESPONSIBLE FOR VERIFYING FINAL COVER DEPTH AND CONSULTING WAVIN DIRECTLY FOR ANY "
    "MULTI-LAYER TANK CONFIGURATION."
)
_MIN_COVER_BURIAL_BOLD_TRIGGERS = ('THE ENGINEER OF RECORD',)


def build_min_cover_burial_pdf(inputs, results, project_name=None):
    """Single-page submittal-ready PDF for the Min Cover / Burial Depth calculator."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'Min Cover / Burial Depth \u2014 Wavin AQ-100-08, Rev 2')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 10
    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    geom_rows = [
        ('Configuration',        'SC — Standard' if results['config'] == 'SC' else 'EX — Extra Strong'),
        ('Traffic Load',         results['traffic_load']),
        ('Layers',               str(results['layers'])),
        ('Tank Height',          f"{results['tank_height_ft']} ft"),
        ('Proposed Cover Depth', f"{results['cover_ft']} ft"),
        ('Total Excavation Depth', f"{results['total_depth_ft']} ft"),
    ]
    y = _draw_section_kv(c, y, 'TANK GEOMETRY', geom_rows, ncols=2)

    result_rows = [
        ('Min. Required Cover',  f"{results['min_cover_ft']:.2f} ft ({results['min_cover_in']:.0f} in)"),
        ('Min. Cover Status',    results['min_status']),
        ('Max. Allowed Cover (single-layer)', f"{results['max_cover_ft']} ft"),
        ('Max. Cover Status',    results['max_status_label']),
    ]
    y = _draw_section_kv(c, y, 'RESULTS', result_rows, ncols=2)

    warnings = results.get('warnings') or []
    if warnings:
        bar_h = 16
        c.setFillColor(AMBER)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'FLAGS')
        warn_top = y - bar_h
        line_h = 11
        pad = 8
        from reportlab.pdfbase.pdfmetrics import stringWidth
        max_w = CW - pad * 2
        wrapped = []
        for w_text in warnings:
            words = w_text.split()
            cur = ''
            for wd in words:
                test = (cur + ' ' + wd).strip()
                if stringWidth(test, 'Helvetica', 7.5) <= max_w:
                    cur = test
                else:
                    if cur:
                        wrapped.append(cur)
                    cur = wd
            if cur:
                wrapped.append(cur)
        warn_h = len(wrapped) * line_h + pad * 2
        warn_bottom = warn_top - warn_h
        c.setFillColor(LTAMB)
        c.setStrokeColor(AMBER)
        c.setLineWidth(0.6)
        c.rect(LM, warn_bottom, CW, warn_h, fill=1, stroke=1)
        ty = warn_top - pad - 6
        c.setFont('Helvetica', 7.5)
        c.setFillColor(BLACK)
        for line in wrapped:
            c.drawString(LM + pad, ty, line)
            ty -= line_h
        y = warn_bottom - 10

    disc_h = _disclaimer_box_height(_MIN_COVER_BURIAL_DISCLAIMER_TEXT)
    disc_top = 40 + disc_h
    available = y - disc_top - 10
    if available >= 90:
        diagram_h = min(available - 16, 150)
        bar_h = 16
        c.setFillColor(BLUE)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'COMPLIANCE GAUGE (NOT TO SCALE)')
        diagram_top = y - bar_h
        diagram_bottom = diagram_top - diagram_h
        c.setFillColor(LGRAY)
        c.setStrokeColor(MGRAY)
        c.setLineWidth(0.5)
        c.rect(LM, diagram_bottom, CW, diagram_h, fill=1, stroke=1)
        _draw_min_cover_gauge_pdf(c, LM, diagram_top - 6, CW, diagram_h - 10, results)
        y = diagram_bottom - 10

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_MIN_COVER_BURIAL_DISCLAIMER_TEXT,
                            bold_triggers=_MIN_COVER_BURIAL_BOLD_TRIGGERS)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


_MIN_DIST_STRUCT_DISCLAIMER_TEXT = (
    "DISCLAIMER: This setback is a preliminary geometric screen based on a single soil "
    "load-influence wedge projected from the adjacent foundation base at the entered internal "
    "shear angle. It is NOT a geotechnical or structural design and does not account for surcharge "
    "loads, groundwater, sloped grade, layered or cohesive soils, wall/footing eccentricity, or "
    "global stability. The Engineer of Record and, where warranted, a licensed geotechnical "
    "engineer are solely responsible for confirming the actual angle of influence, soil parameters, "
    "and site conditions. FINAL SETBACKS, SHORING, AND LOAD-TRANSFER DETAILS MUST BE CONFIRMED BY "
    "A LICENSED PROFESSIONAL ENGINEER using project-specific plans and soils data."
)
_MIN_DIST_STRUCT_BOLD_TRIGGERS = ('FINAL SETBACKS', 'LICENSED PROFESSIONAL', 'NOT a geotechnical')


def _draw_min_dist_struct_diagram_pdf(c, x0, y0, w_area, h_area, r):
    """Scaled cross-section: finish grade line, adjacent foundation to depth
    H1, tank base to depth H2, and the wedge line rising from the tank base
    at angle 'a' to grade. The horizontal run to grade = L_min. Feet-honest
    within the drawable box."""
    h1 = r['h1_found_depth_ft']
    h2 = r['h2_tank_depth_ft']
    l_min = r['l_min_ft']
    angle = r['shear_angle_deg']

    # Model space (feet): x from 0 (foundation face) rightward; y downward = depth.
    # Show a little structure width to the left and a little tank width to the right.
    struct_w = max(h2 * 0.35, 2.0)
    tank_w = max(l_min * 0.9, h2 * 0.6, 3.0)
    total_w_ft = struct_w + max(l_min, 0.001) + tank_w
    total_h_ft = max(h2 * 1.25, 1.0)

    pad = 16
    draw_w = w_area - pad * 2
    draw_h = h_area - pad * 2
    if total_w_ft <= 0 or total_h_ft <= 0:
        return
    s = min(draw_w / total_w_ft, draw_h / total_h_ft)   # ft -> pt

    # Origin: foundation face at model x=struct_w; grade at top of drawable area.
    ox = x0 + pad
    grade_y = y0 + h_area - pad
    def X(ft): return ox + ft * s
    def Y(depth_ft): return grade_y - depth_ft * s   # depth grows downward

    found_face_x = struct_w  # model-x of the foundation right face / tank-side

    # Finish grade line
    c.setStrokeColor(GRAY); c.setLineWidth(0.8); c.setDash()
    c.line(X(0), grade_y, X(total_w_ft), grade_y)
    c.setFillColor(GRAY); c.setFont('Helvetica', 6.5)
    c.drawString(X(0), grade_y + 3, 'FINISH GRADE')

    # Adjacent structure (foundation) block
    c.setFillColor(MGRAY); c.setStrokeColor(GRAY); c.setLineWidth(0.7)
    c.rect(X(0), Y(h1), (found_face_x) * s, h1 * s, fill=1, stroke=1)
    c.setFillColor(BLACK); c.setFont('Helvetica-Bold', 6.5)
    c.drawCentredString(X(found_face_x / 2.0), Y(h1 / 2.0), 'STRUCTURE')
    c.setFont('Helvetica', 6)
    c.drawCentredString(X(found_face_x / 2.0), Y(h1 / 2.0) - 8, f'H1 = {h1:.2f} ft')

    # Tank block (starts at required setback from foundation face)
    tank_left = found_face_x + max(l_min, 0.0)
    c.setFillColor(LTBLUE); c.setStrokeColor(BLUE); c.setLineWidth(0.9)
    c.rect(X(tank_left), Y(h2), tank_w * s, (h2 - 0.0) * s, fill=1, stroke=1)
    c.setFillColor(DKBLUE); c.setFont('Helvetica-Bold', 6.5)
    c.drawCentredString(X(tank_left + tank_w / 2.0), Y(h2 * 0.5), 'AquaCell')
    c.setFillColor(BLACK); c.setFont('Helvetica', 6)
    c.drawCentredString(X(tank_left + tank_w / 2.0), Y(h2 * 0.5) - 8, f'H2 = {h2:.2f} ft')

    # Wedge line: from tank base (tank_left, h2) up to grade at angle 'a'.
    # Horizontal run to grade = l_min, landing at model-x = found_face_x.
    c.setStrokeColor(RED); c.setLineWidth(1.3); c.setDash(3, 2)
    c.line(X(tank_left), Y(h2), X(found_face_x), Y(0))
    c.setDash()
    c.setFillColor(RED); c.setFont('Helvetica-Oblique', 6)
    c.drawString(X((tank_left + found_face_x) / 2.0) + 2,
                 Y(h2 / 2.0) + 2, f'wedge {angle:.0f}\u00b0')

    # L_min dimension arrow along grade between foundation face and tank face
    dim_y = grade_y - 10
    c.setStrokeColor(DKBLUE); c.setLineWidth(0.8)
    c.line(X(found_face_x), dim_y, X(tank_left), dim_y)
    for xx in (found_face_x, tank_left):
        c.line(X(xx), dim_y - 3, X(xx), dim_y + 3)
    c.setFillColor(DKBLUE); c.setFont('Helvetica-Bold', 7)
    c.drawCentredString(X((found_face_x + tank_left) / 2.0), dim_y - 10,
                        f'L min = {l_min:.2f} ft')


def build_min_dist_struct_pdf(inputs, results, project_name=None):
    """Single-page submittal-ready PDF for the Min Distance from Structure
    (building / footing / retaining-wall setback) calculator."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'Min Distance from Structure \u2014 Foundation Setback')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 10
    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    # Hero: required setback + PASS/FAIL if a proposed distance was given
    status = results.get('status')
    hero_status = status if status in ('PASS', 'FAIL') else 'NA'
    hero_val = f"{results['l_min_ft']:.2f} ft"
    if results['wedge_clears']:
        sub = 'No setback required \u2014 tank base at/above foundation base'
    else:
        sub = f"Minimum horizontal setback ({results['l_min_in']:.0f} in) from structure to tank"
    y = _draw_fos_hero(c, y, hero_val, sub, hero_status)

    in_rows = [
        ('Foundation Depth (H1)', f"{results['h1_found_depth_ft']:.2f} ft"),
        ('Tank Base Depth (H2)',  f"{results['h2_tank_depth_ft']:.2f} ft"),
        ('Internal Shear Angle (a)', f"{results['shear_angle_deg']:.1f}\u00b0"),
        ('Depth Difference (H2\u2212H1)', f"{results['depth_diff_ft']:.2f} ft"),
    ]
    y = _draw_section_kv(c, y, 'INPUTS', in_rows, ncols=2)

    res_rows = [('Required Min. Setback (L min)',
                 f"{results['l_min_ft']:.2f} ft ({results['l_min_in']:.0f} in)")]
    if results.get('proposed_distance_ft') is not None:
        res_rows.append(('Proposed Distance', f"{results['proposed_distance_ft']:.2f} ft"))
        res_rows.append(('Margin (Proposed \u2212 Required)', f"{results['margin_ft']:.2f} ft"))
        res_rows.append(('Setback Check', status or 'NA'))
    y = _draw_section_kv(c, y, 'RESULTS', res_rows, ncols=2)

    # Formula line
    c.setFillColor(GRAY); c.setFont('Helvetica-Oblique', 7.5)
    c.drawString(LM, y - 2, 'Method: L min = (H2 \u2212 H1) \u00d7 tan(90\u00b0 \u2212 a) = (H2 \u2212 H1) \u00f7 tan(a). '
                 'Internal Wavin method (no standalone drawing number).')
    y -= 16

    # Notes / flags
    notes = results.get('notes') or []
    if notes:
        bar_h = 16
        c.setFillColor(AMBER)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE); c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'NOTES / FLAGS')
        warn_top = y - bar_h
        line_h = 11; pad = 8
        from reportlab.pdfbase.pdfmetrics import stringWidth
        max_w = CW - pad * 2
        wrapped = []
        for w_text in notes:
            words = w_text.split(); cur = ''
            for wd in words:
                test = (cur + ' ' + wd).strip()
                if stringWidth(test, 'Helvetica', 7.5) <= max_w:
                    cur = test
                else:
                    if cur:
                        wrapped.append(cur)
                    cur = wd
            if cur:
                wrapped.append(cur)
        warn_h = len(wrapped) * line_h + pad * 2
        warn_bottom = warn_top - warn_h
        c.setFillColor(LTAMB); c.setStrokeColor(AMBER); c.setLineWidth(0.6)
        c.rect(LM, warn_bottom, CW, warn_h, fill=1, stroke=1)
        ty = warn_top - pad - 6
        c.setFont('Helvetica', 7.5); c.setFillColor(BLACK)
        for line in wrapped:
            c.drawString(LM + pad, ty, line)
            ty -= line_h
        y = warn_bottom - 10

    # Cross-section diagram (if room)
    disc_h = _disclaimer_box_height(_MIN_DIST_STRUCT_DISCLAIMER_TEXT)
    disc_top = 40 + disc_h
    available = y - disc_top - 10
    if available >= 90:
        diagram_h = min(available - 16, 175)
        bar_h = 16
        c.setFillColor(BLUE)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE); c.setFont('Helvetica-Bold', 9)
        c.drawString(LM + 8, y - bar_h + 4, 'CROSS-SECTION (NOT TO SCALE)')
        diagram_top = y - bar_h
        diagram_bottom = diagram_top - diagram_h
        c.setFillColor(LGRAY); c.setStrokeColor(MGRAY); c.setLineWidth(0.5)
        c.rect(LM, diagram_bottom, CW, diagram_h, fill=1, stroke=1)
        _draw_min_dist_struct_diagram_pdf(c, LM, diagram_bottom, CW, diagram_h, results)
        y = diagram_bottom - 10

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_MIN_DIST_STRUCT_DISCLAIMER_TEXT,
                            bold_triggers=_MIN_DIST_STRUCT_BOLD_TRIGGERS)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


_COMPLEX_SHAPE_DISCLAIMER_TEXT = (
    "This conceptual schematic is a preliminary planning aid only and is NOT a stamped engineering "
    "design or a construction layout. Crate counts and footprint areas are approximate: envelope "
    "(fit-to-footprint) mode packs whole rectangular modules into the entered dimensions and does not "
    "account for stepped corners, angled edges, interior obstructions, or field trimming. The Engineer "
    "of Record and a project-specific takeoff (e.g. CAD / Bluebeam) remain the source of truth for final "
    "quantities. AquaCell module dimensions follow published product data. FINAL LAYOUTS, CAPACITIES, "
    "AND INSTALLATION DEPTHS MUST BE CONFIRMED BY A LICENSED PROFESSIONAL ENGINEER."
)
_COMPLEX_SHAPE_BOLD_TRIGGERS = ['NOT', 'source of truth', 'MUST BE CONFIRMED BY A LICENSED PROFESSIONAL ENGINEER']


def _draw_complex_shape_plan_pdf(c, x0, y0, w_area, h_area, results):
    """Draw the plan-view footprint from the row data, scaled to fit the
    given drawable box (x0,y0 = bottom-left; w_area,h_area = box size).
    Mirrors the on-screen SVG: shaded rows, crate grid, thick perimeter
    outline, dashed bounding box, and a scale bar. Feet-honest scaling."""
    MW = results['module_wid_ft']
    ML = results['module_len_ft']
    rows = results['rows']
    n_rows = len(rows)
    if n_rows == 0:
        return

    max_col = max(r['offset_crates'] + r['crate_count'] for r in rows)
    width_ft = max_col * MW
    length_ft = n_rows * ML

    # Reserve strip at bottom for scale bar.
    bar_strip = 26
    draw_h = h_area - bar_strip
    draw_w = w_area
    s = min(draw_w / width_ft, draw_h / length_ft)   # ft -> pt, uniform

    shape_w = width_ft * s
    shape_h = length_ft * s
    # center in the box
    ox = x0 + (w_area - shape_w) / 2.0
    oy = y0 + bar_strip + (draw_h - shape_h) / 2.0

    def px(col): return ox + col * MW * s
    def py_top(row): return oy + shape_h - row * ML * s   # row 0 at top

    # Dashed bounding box
    c.setStrokeColor(DKBLUE)
    c.setLineWidth(0.7)
    c.setDash(4, 3)
    c.rect(ox, oy, shape_w, shape_h, fill=0, stroke=1)
    c.setDash()

    # Row fills
    c.setFillColor(LTBLUE)
    for i, r in enumerate(rows):
        x = px(r['offset_crates'])
        yt = py_top(i)
        w = r['crate_count'] * MW * s
        h = ML * s
        c.rect(x, yt - h, w, h, fill=1, stroke=0)

    # Crate grid (light)
    c.setStrokeColor(MGRAY)
    c.setLineWidth(0.3)
    for i, r in enumerate(rows):
        yt = py_top(i)
        yb = yt - ML * s
        for cc in range(r['offset_crates'], r['offset_crates'] + r['crate_count'] + 1):
            gx = px(cc)
            c.line(gx, yb, gx, yt)
        c.line(px(r['offset_crates']), yb, px(r['offset_crates'] + r['crate_count']), yb)
        c.line(px(r['offset_crates']), yt, px(r['offset_crates'] + r['crate_count']), yt)

    # Perimeter outline (thick) — exposed boundary edges of the union
    filled = set()
    for i, r in enumerate(rows):
        for cc in range(r['offset_crates'], r['offset_crates'] + r['crate_count']):
            filled.add((i, cc))
    def has(rr, cc): return (rr, cc) in filled
    c.setStrokeColor(BLUE)
    c.setLineWidth(2)
    for i, r in enumerate(rows):
        yt = py_top(i)
        yb = yt - ML * s
        for cc in range(r['offset_crates'], r['offset_crates'] + r['crate_count']):
            xl = px(cc); xr = px(cc + 1)
            if not has(i - 1, cc): c.line(xl, yt, xr, yt)          # top
            if not has(i + 1, cc): c.line(xl, yb, xr, yb)          # bottom
            if not has(i, cc - 1): c.line(xl, yb, xl, yt)          # left
            if not has(i, cc + 1): c.line(xr, yb, xr, yt)          # right

    # Dimension labels
    c.setFillColor(GRAY)
    c.setFont('Helvetica', 7)
    c.drawCentredString(ox + shape_w / 2, oy + shape_h + 4,
                        f"{round(width_ft,2)} ft ({max_col} crates)")
    c.saveState()
    c.translate(ox - 6, oy + shape_h / 2)
    c.rotate(90)
    c.drawCentredString(0, 0, f"{round(length_ft,2)} ft ({n_rows} rows)")
    c.restoreState()

    # Scale bar: pick a round footage that fits
    candidates = [5, 10, 25, 50, 100, 200]
    bar_ft = candidates[0]
    for cand in candidates:
        if cand * s <= shape_w * 0.6:
            bar_ft = cand
    bar_px = bar_ft * s
    bx = x0 + 4
    by = y0 + 8
    c.setStrokeColor(BLACK)
    c.setLineWidth(1)
    c.setFillColor(BLACK)
    c.rect(bx, by, bar_px, 4, fill=1, stroke=0)
    c.setFont('Helvetica', 7)
    c.drawString(bx, by + 8, f"{bar_ft} ft")
    c.setFillColor(GRAY)
    c.drawRightString(x0 + w_area, by + 8,
                      f"1 crate = {round(ML,3)} ft x {round(MW,4)} ft")


def build_complex_shape_pdf(inputs, results, project_name=None):
    """Two-page conceptual submittal PDF for the Complex Shape Builder.
    Page 1: configuration + derived quantities. Page 2: scaled plan-view."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    def header(subtitle):
        band_h = 64
        c.setFillColor(NAVY)
        c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
        logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
        if logo_path and os.path.exists(logo_path):
            try:
                img = ImageReader(logo_path)
                c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                            preserveAspectRatio=True, mask='auto')
            except Exception:
                pass
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 14)
        c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
        c.setFont('Helvetica', 9)
        c.setFillColor(colors.HexColor('#93c5fd'))
        c.drawRightString(PW - RM, PH - 37, subtitle)
        c.setFont('Helvetica', 7)
        c.setFillColor(colors.HexColor('#94a3b8'))
        c.drawRightString(PW - RM, PH - 50, generated_str)
        return PH - band_h - 10

    # ── PAGE 1 ──────────────────────────────────────────────────────
    y = header('Complex Shape Builder \u2014 Conceptual Schematic')
    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    mode = results.get('mode', 'rows')
    mode_label = 'Fit to Footprint (envelope)' if mode == 'envelope' else 'Build by Rows'
    cfg_rows = [
        ('Configuration', 'SC — Standard' if results['config'] == 'SC' else 'EX — Extra Strong'),
        ('Build Mode', mode_label),
        ('Number of Layers', str(results['layers'])),
        ('Tank Height', f"{results['tank_height_ft']} ft"),
        ('Number of Rows', str(results['n_rows'])),
        ('Total Crates / Layer', f"{results['total_crates_layer']:,}"),
    ]
    y = _draw_section_kv(c, y, 'SYSTEM CONFIGURATION', cfg_rows, ncols=2)

    if mode == 'envelope' and results.get('envelope'):
        e = results['envelope']
        orient_label = ('Long axis down length' if e['orientation'] == 'len_along_length'
                        else 'Long axis across width')
        env_rows = [
            ('Entered Footprint', f"{e['entered_width_ft']} ft x {e['entered_length_ft']} ft"),
            ('Used Footprint (snapped down)', f"{e['used_width_ft']} ft x {e['used_length_ft']} ft"),
            ('Crate Orientation', orient_label),
            ('Crates — long axis down length', f"{e['crates_len_along_length']:,}"),
            ('Crates — long axis across width', f"{e['crates_len_along_width']:,}"),
            ('Active Count', f"{results['total_crates_layer']:,} / layer"),
        ]
        y = _draw_section_kv(c, y, 'ENVELOPE / FOOTPRINT', env_rows, ncols=2)

    qty_rows = [
        ('Net Footprint Area', f"{results['net_footprint_area_sf']:,.2f} sf"),
        ('Bounding Box Area', f"{results['bounding_box_area_sf']:,.2f} sf"),
        ('Fill Efficiency', f"{results['fill_efficiency_pct']}%"),
        ('Perimeter', f"{results['perimeter_ft']:,.2f} ft"),
        ('Total Crates (all layers)', f"{results['total_crates_all_layers']:,}"),
        ('Bounding W x L', f"{results['bounding_width_ft']} x {results['bounding_length_ft']} ft"),
    ]
    y = _draw_section_kv(c, y, 'DERIVED QUANTITIES', qty_rows, ncols=2)

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_COMPLEX_SHAPE_DISCLAIMER_TEXT,
                            bold_triggers=_COMPLEX_SHAPE_BOLD_TRIGGERS)
    c.showPage()

    # ── PAGE 2 ──────────────────────────────────────────────────────
    y = header('Conceptual Plan View (Top-Down)')
    c.setFillColor(GRAY)
    c.setFont('Helvetica-Bold', 10)
    title_bits = f"AquaCell {results['config']} | {results['bounding_width_ft']} x {results['bounding_length_ft']} ft | {results['total_crates_layer']:,} crates/layer"
    c.drawString(LM, y, title_bits)
    y -= 6

    bar_h = 16
    c.setFillColor(BLUE)
    c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(LM + 8, y - bar_h + 4, 'PLAN VIEW \u2014 SHADED = CRATE MODULES, BLUE OUTLINE = PERIMETER')
    box_top = y - bar_h

    disc_h = _disclaimer_box_height(_COMPLEX_SHAPE_DISCLAIMER_TEXT)
    box_bottom = 40 + disc_h + 10
    box_h = box_top - box_bottom
    c.setFillColor(WHITE)
    c.setStrokeColor(MGRAY)
    c.setLineWidth(0.5)
    c.rect(LM, box_bottom, CW, box_h, fill=1, stroke=1)

    _draw_complex_shape_plan_pdf(c, LM + 10, box_bottom + 6, CW - 20, box_h - 12, results)

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_COMPLEX_SHAPE_DISCLAIMER_TEXT,
                            bold_triggers=_COMPLEX_SHAPE_BOLD_TRIGGERS)
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


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
    geoWaste         = int(t.get('geoWaste', 20) or 20)
    pipe_connectors  = int(t.get('pipe_connectors', 0) or 0)
    top_adapters_12  = int(t.get('top_adapters_12', 0) or 0)
    top_adapters_16  = int(t.get('top_adapters_16', 0) or 0)
    min_storage      = float(t.get('min_storage', 0) or 0)
    tank_label       = t.get('tank_label', 'Tank')
    tank_notes       = (t.get('tank_notes', '') or '').strip()

    # ── Optional accessories ─────────────────────────────────────────
    geogrid_top_yd2    = int(t.get('geogrid_top_yd2',    0) or 0)
    geogrid_bottom_yd2 = int(t.get('geogrid_bottom_yd2', 0) or 0)
    large_pipe_18   = int(t.get('large_pipe_18',   0) or 0)
    large_pipe_24   = int(t.get('large_pipe_24',   0) or 0)
    large_pipe_36   = int(t.get('large_pipe_36',   0) or 0)
    large_pipe_gt36 = int(t.get('large_pipe_gt36', 0) or 0)
    large_pipe_qty  = large_pipe_18 + large_pipe_24 + large_pipe_36 + large_pipe_gt36 or int(t.get('large_pipe_qty', 0) or 0)
    liner_on_tank      = bool(t.get('liner_on_tank',  False))
    liner_on_stone     = bool(t.get('liner_on_stone', False))
    ptrow_enabled      = bool(t.get('ptrow_enabled', False))
    ptrow_method       = t.get('ptrow_method', 'volume')
    ptrow_areas        = t.get('ptrow_areas', [])
    ptrow_flow_areas   = t.get('ptrow_flow_areas', [])
    FLOW_COEFF         = 0.464

    layer_heights   = cd['layer_heights']
    void_ratio      = cd['void_ratio']
    side_multiplier = cd['side_multiplier']

    tank_height        = layer_heights[layers - 1] if layers <= len(layer_heights) else layer_heights[-1]
    total_system_depth = base_stone + tank_height + cover_stone

    shape_mode = t.get('shape_mode', 'rectangle')

    if shape_mode == 'complex':
        complex_scaled_area  = float(t.get('complex_scaled_area',  0) or 0)
        complex_known_dim    = float(t.get('complex_known_dim',    0) or 0)
        complex_tank_perim   = float(t.get('complex_tank_perim',   0) or 0)
        complex_excav_area   = float(t.get('complex_excav_area',   0) or 0)
        complex_excav_perim  = float(t.get('complex_excav_perim',  0) or 0)

        other_dim    = complex_scaled_area / complex_known_dim if complex_known_dim > 0 else 0
        crates_known = math.floor(complex_known_dim / MODULE_WID) if complex_known_dim > 0 else 0
        crates_other = math.floor(other_dim / MODULE_LEN) if other_dim > 0 else 0
        snapped_known = crates_known * MODULE_WID
        snapped_other = crates_other * MODULE_LEN

        complex_tank_area  = snapped_known * snapped_other
        gross_tank_vol     = complex_tank_area * tank_height
        tank_storage       = gross_tank_vol * void_ratio
        crates_layer       = crates_known * crates_other
        num_crates         = crates_layer * layers
        crates_wide        = crates_known   # alias for display consistency
        crates_long        = crates_other   # alias for display consistency

        if complex_excav_area <= 0:
            complex_excav_area = (snapped_known + 2*perimeter_stone_width) * (snapped_other + 2*perimeter_stone_width)
        if complex_excav_perim <= 0:
            complex_excav_perim = complex_tank_perim + 8*perimeter_stone_width

        total_excav_vol  = complex_excav_area * total_system_depth
        stone_env_vol    = total_excav_vol - gross_tank_vol
        total_stone_stor = stone_env_vol * stone_void
        total_storage    = tank_storage + total_stone_stor

        used_perimeter   = round(complex_tank_perim, 2)
        tank_width       = round(snapped_known, 2)
        tank_length      = round(snapped_other, 2)
        tank_perim       = complex_tank_perim
        outer_width      = round(snapped_known + 2*perimeter_stone_width, 4)
        outer_length     = round(snapped_other + 2*perimeter_stone_width, 4)

    else:
        # Rectangle geometry
        crates_wide    = math.floor(known_width  / MODULE_WID)
        crates_long    = math.floor(known_length / MODULE_LEN)
        tank_width     = crates_wide * MODULE_WID
        tank_length    = crates_long * MODULE_LEN
        crates_layer   = crates_wide * crates_long
        num_crates     = crates_layer * layers
        gross_tank_vol = tank_width * tank_length * tank_height
        tank_storage   = gross_tank_vol * void_ratio

        outer_width    = tank_width  + 2 * perimeter_stone_width
        outer_length   = tank_length + 2 * perimeter_stone_width
        total_excav_vol   = outer_width * outer_length * total_system_depth
        stone_env_vol     = total_excav_vol - gross_tank_vol
        total_stone_stor  = stone_env_vol * stone_void
        total_storage     = tank_storage + total_stone_stor

        used_perimeter = 2 * (tank_width + tank_length)
        tank_perim     = used_perimeter

        complex_scaled_area = complex_known_dim = complex_tank_perim = None
        complex_excav_area  = complex_excav_perim = None
        complex_tank_area   = None
    side_plates    = round(tank_perim * (layers * side_multiplier) / 5.17)

    if config == 'SC':
        base_units    = num_crates
        bottom_plates = crates_layer
    else:
        base_units    = num_crates * 2
        bottom_plates = 0

    # Contingency: round up base_units to next full pallet
    contingency = max(0, math.ceil(base_units / _MT_PALLETS['base']) * _MT_PALLETS['base'] - base_units)

    # Geotextile
    excav_area = outer_width * outer_length if shape_mode != 'complex' else (complex_excav_area or 0)
    if shape_mode == 'complex':
        geoTank  = round((2*(complex_tank_area or 0) + (complex_tank_perim or 0)*tank_height) * (1 + geoWaste/100.0), 1)
        geoStone = round((2*(complex_excav_area or 0) + (complex_excav_perim or 0)*total_system_depth) * (1 + geoWaste/100.0), 1)
    else:
        tank_top_bottom = 2 * tank_width * tank_length
        tank_sides      = 2 * (tank_width + tank_length) * tank_height
        geoTank  = round((tank_top_bottom + tank_sides) * (1 + geoWaste/100.0), 1)
        geoStone = round((excav_area*2 + outer_width*total_system_depth*2 + outer_length*total_system_depth*2) * (1 + geoWaste/100.0), 1)
    geoTotal = round(geoTank + geoStone, 1)

    # ── Non-woven deduction: geogrid bottom substitutes tank floor fabric ──
    geoTank_yd2     = round(geoTank  / 9, 1)
    geoStone_yd2    = round(geoStone / 9, 1)
    geoTotal_yd2    = round(geoTotal / 9, 1)
    geoTank_yd2_adj = max(0, round(geoTank_yd2 - geogrid_bottom_yd2, 1))

    # ── PT-ROW™ fabric (taco wrap: bottom + 2 long sides + back end) ──
    _ptrow_layer_ht  = layer_heights[0]
    _ptrow_wrap_ext  = 1.5
    _ptrow_crate_vol = round(MODULE_WID * MODULE_LEN * _ptrow_layer_ht * void_ratio, 4)

    ptrow_total_crates = 0
    if ptrow_enabled:
        if ptrow_method == 'flow' and ptrow_flow_areas:
            for area in ptrow_flow_areas:
                cfs = float(area.get('cfs', 0) or 0)
                ptrow_total_crates += math.ceil(cfs / FLOW_COEFF) if FLOW_COEFF > 0 else 0
        elif ptrow_areas:
            for area in ptrow_areas:
                wqv = float(area.get('wqv', 0) or 0)
                pct = float(area.get('pct', 10) or 10)
                pt_vol = wqv * pct / 100.0
                ptrow_total_crates += math.ceil(pt_vol / _ptrow_crate_vol) if _ptrow_crate_vol > 0 else 0

    if ptrow_enabled and ptrow_total_crates > 0:
        _fab_w          = MODULE_WID + 2*_ptrow_layer_ht + 2*_ptrow_wrap_ext
        _fab_l          = ptrow_total_crates * MODULE_LEN + 2*_ptrow_wrap_ext
        ptrow_woven_yd2 = math.ceil(_fab_w * _fab_l * (1 + geoWaste/100.0) / 9)
    else:
        ptrow_woven_yd2 = 0

    # ── PVC / Geomembrane liner ────────────────────────────────────────
    liner_tank_yd2  = math.ceil(geoTank  * (1 + geoWaste/100.0) / 9) if liner_on_tank  else 0
    liner_stone_yd2 = math.ceil(geoStone * (1 + geoWaste/100.0) / 9) if liner_on_stone else 0
    liner_total_yd2 = liner_tank_yd2 + liner_stone_yd2
    stone_yd3  = round(stone_env_vol * 1.10 / 27, 1)
    stone_tons = round(stone_env_vol * 1.10 * 100 / 2000, 2)

    # Stone by layer
    stone_top_gross   = round(excav_area * cover_stone, 1)
    stone_top_net     = round(stone_top_gross * stone_void, 1)
    stone_perim_gross = round((excav_area - tank_width * tank_length) * tank_height, 1)
    stone_perim_net   = round(stone_perim_gross * stone_void, 1)
    stone_base_gross  = round(excav_area * base_stone, 1)
    stone_base_net    = round(stone_base_gross * stone_void, 1)

    # Per-layer net-storage inclusion toggles (default = included). The
    # included-layer net sum is the authoritative stone storage (matches the
    # single-tank model). Excluding a layer drops its storage CREDIT only —
    # the stone is still placed, so stone_yd3 / stone_tons stay gross-based.
    def _inc(key):
        v = t.get(key, True)
        if isinstance(v, str):
            return v == '1' or v.lower() == 'true'
        return bool(v)
    stone_top_included   = _inc('stone_top_included')
    stone_perim_included = _inc('stone_perim_included')
    stone_base_included  = _inc('stone_base_included')
    stone_layer_total_net = round(
        (stone_top_net   if stone_top_included   else 0.0) +
        (stone_perim_net if stone_perim_included else 0.0) +
        (stone_base_net  if stone_base_included  else 0.0), 1)
    total_stone_stor = stone_layer_total_net
    total_storage    = round(tank_storage + total_stone_stor, 1)

    # Cover / load
    cover_depth = round(surface_elev - (tank_bottom_elev + tank_height), 2)
    min_cover   = cd['min_cover'].get(traffic_load, 1.0)
    max_cover   = cd['max_cover']
    cover_ok    = cover_depth >= min_cover
    max_cover_ok = cover_depth <= max_cover

    dead_load_psi   = round(cover_stone * 120 / 144, 2)
    max_str         = cd['max_strength']
    fos_dead        = round(max_str / dead_load_psi, 2) if dead_load_psi > 0 else None

    # ASTM F2787 live load — uses the verified calc_astm_f2787_truck() formulas
    # (fixes the IM-depth-dependency + spread-formula bugs from the old inline
    # copy), while preserving this function's existing split of cover_stone
    # (dead load, above) vs. cover_depth (live load, below) exactly as before —
    # only the live-load FORMULA changed, not which cover variable feeds which output.
    _ll_detail = calc_astm_f2787_truck(traffic_load, cover_depth, config) if cover_depth > 0 else None
    ll_psi = _ll_detail['ll_psi'] if _ll_detail else None
    total_press = round((ll_psi or 0) + dead_load_psi, 3)
    fos_ll = round(max_str / total_press, 2) if total_press > 0 else None

    storage_ok = (total_storage >= min_storage) if min_storage > 0 else None

    return {
        # Identity
        'tank_label':      tank_label,
        'tank_notes':      tank_notes,
        'config':          config,
        'layers':          layers,
        'shape_mode':      shape_mode,
        'complex_scaled_area':  complex_scaled_area  if shape_mode == 'complex' else None,
        'complex_known_dim':    complex_known_dim     if shape_mode == 'complex' else None,
        'complex_tank_perim':   complex_tank_perim    if shape_mode == 'complex' else None,
        'complex_excav_area':   complex_excav_area    if shape_mode == 'complex' else None,
        'complex_excav_perim':  complex_excav_perim   if shape_mode == 'complex' else None,
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
        'geoTank':         geoTank,
        'geoStone':        geoStone,
        'geoTotal':        geoTotal,
        'geoTank_yd2':     geoTank_yd2,
        'geoStone_yd2':    geoStone_yd2,
        'geoTotal_yd2':    geoTotal_yd2,
        'geoTank_yd2_adj': geoTank_yd2_adj,
        'geoWaste':        geoWaste,
        # PT-ROW™
        'ptrow_enabled':      ptrow_enabled,
        'ptrow_method':       ptrow_method,
        'ptrow_areas':        ptrow_areas,
        'ptrow_flow_areas':   ptrow_flow_areas,
        'ptrow_total_crates': ptrow_total_crates,
        'ptrow_woven_yd2':    ptrow_woven_yd2,
        'ptrow_crate_vol':    _ptrow_crate_vol,
        'ptrow_layer_ht':     round(_ptrow_layer_ht, 4),
        # Geogrid
        'geogrid_top_yd2':    geogrid_top_yd2,
        'geogrid_bottom_yd2': geogrid_bottom_yd2,
        # Liner
        'liner_on_tank':   liner_on_tank,
        'liner_on_stone':  liner_on_stone,
        'liner_tank_yd2':  liner_tank_yd2,
        'liner_stone_yd2': liner_stone_yd2,
        'liner_total_yd2': liner_total_yd2,
        # Liner-protection non-woven (AQ-100-03.4 Note C): one matching
        # non-woven layer per selected liner envelope. Equals the liner qty.
        # Powers the dashboard rail/cumulative rows; the quote PDF Line I
        # derives the same value independently from cum_liner_total.
        'liner_prot_total_yd2': liner_total_yd2,
        # Large pipe
        'large_pipe_qty': large_pipe_qty,
        # Stone backfill
        'stone_yd3':       stone_yd3,
        'stone_tons':      stone_tons,
        'stone_top_gross': stone_top_gross,  'stone_top_net':   stone_top_net,
        'stone_perim_gross': stone_perim_gross, 'stone_perim_net': stone_perim_net,
        'stone_base_gross': stone_base_gross,  'stone_base_net':  stone_base_net,
        'stone_top_included':   stone_top_included,
        'stone_perim_included': stone_perim_included,
        'stone_base_included':  stone_base_included,
        'stone_layer_total_net': stone_layer_total_net,
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
        'geoTank_yd2': 0.0, 'geoStone_yd2': 0.0, 'geoTotal_yd2': 0.0,
        'geoTank_yd2_adj': 0.0,
        'stone_yd3': 0.0, 'stone_tons': 0.0,
        'ptrow_total_crates': 0, 'ptrow_woven_yd2': 0,
        'geogrid_top_yd2': 0, 'geogrid_bottom_yd2': 0,
        'liner_tank_yd2': 0, 'liner_stone_yd2': 0, 'liner_total_yd2': 0,
        'liner_prot_total_yd2': 0,
        'large_pipe_qty': 0,
    }
    for r in tank_results:
        for k in bom:
            bom[k] = round(bom[k] + r.get(k, 0), 2)
    # Re-compute cumulative contingency as pallet-rounding of cumulative base_units
    bom['contingency'] = max(0,
        math.ceil(bom['base_units'] / _MT_PALLETS['base']) * _MT_PALLETS['base'] - bom['base_units'])
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

        def bom_row(key, qty):
            w = round(qty * _MT_WEIGHTS.get(key, 0), 1)
            p = math.ceil(qty / _MT_PALLETS[key]) if qty > 0 and key in _MT_PALLETS else 0
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
#  CRATE COMPARISON (BETA)  -  value-engineering estimator
#
#  Compares an AquaCell system against a generic competitor crate
#  system on a QUANTITIES-ONLY basis (no cost), sized to an equal
#  required storage volume. Each system is sized BY ITS OWN RULES:
#  its own per-module storage, its own stone envelope (base / cover /
#  perimeter / stone void). Efficiency is computed identically for
#  both (total storage / total excavated field) so it is a fair,
#  apples-to-apples, this-tool system-scale figure - deliberately
#  NOT the same as a vendor's single-chamber sheet number.
#
#  Mode A: user transcribes a real competitor design; the target
#          storage is derived from that competitor system.
#  Mode B: user types a target storage; the competitor system is
#          ESTIMATED to meet it (clearly stamped, not a vendor design).
#
#  Sizing is client-side JS for the on-screen table; this server
#  helper re-derives the same numbers from raw inputs so the PDF is
#  authoritative and self-contained (never trusts JS-computed values).
# ══════════════════════════════════════════════════════════════════
def _cc_size_system(sysd, required_storage_cf):
    """Size one crate system to meet a required storage volume, by its
    own rules. `sysd` is a per-system input dict. Returns a result dict
    of quantities. Pure geometry; no cost."""
    def f(key, default=0.0):
        try:
            return float(sysd.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    label            = str(sysd.get('label', 'System')).strip() or 'System'
    module_len_ft    = f('module_len_ft')
    module_wid_ft    = f('module_wid_ft')
    module_height_ft = f('module_height_ft')
    storage_per_cf   = f('storage_per_module_cf')
    layers           = max(1, int(f('layers', 1)))
    stone_base_ft    = f('stone_base_ft')
    stone_cover_ft   = f('stone_cover_ft')
    stone_perim_ft   = f('stone_perimeter_ft')
    stone_void_pct   = f('stone_void_pct')

    module_plan_area = module_len_ft * module_wid_ft            # sf (one module footprint)

    # Modules needed to meet required storage (whole modules, snap up).
    if storage_per_cf > 0:
        modules = math.ceil(required_storage_cf / storage_per_cf)
    else:
        modules = 0
    modules = max(modules, 0)

    # Footprint = modules per layer laid out in plan.
    modules_per_layer = math.ceil(modules / layers) if layers > 0 else modules
    footprint_sf      = modules_per_layer * module_plan_area

    # Approximate a square-ish footprint L x W for reporting (informational).
    fp_side = math.sqrt(footprint_sf) if footprint_sf > 0 else 0.0

    system_height_ft = module_height_ft * layers

    # Excavated field: footprint expanded by the stone perimeter on all sides,
    # by the full field depth (base + system height + cover).
    field_len = fp_side + 2 * stone_perim_ft
    field_wid = fp_side + 2 * stone_perim_ft
    field_footprint_sf = field_len * field_wid
    field_depth_ft = stone_base_ft + system_height_ft + stone_cover_ft
    excavation_cf = field_footprint_sf * field_depth_ft

    # Module (crate) storage provided.
    crate_storage_cf = modules * storage_per_cf

    # Module displacement volume (solid bounding volume of all modules).
    module_bounding_cf = module_plan_area * module_height_ft * modules
    # Stone envelope volume = excavated field minus the module bounding volume.
    stone_env_cf = max(excavation_cf - module_bounding_cf, 0.0)
    stone_storage_cf = stone_env_cf * (stone_void_pct / 100.0)

    total_storage_cf = crate_storage_cf + stone_storage_cf

    # System-scale efficiency (this tool's basis, identical both systems).
    efficiency_pct = (total_storage_cf / excavation_cf * 100.0) if excavation_cf > 0 else 0.0

    # Geotextile wrap = outer surface area of the field envelope (all 6 faces).
    wrap_sf = (2 * field_footprint_sf) + (2 * field_len * field_depth_ft) + (2 * field_wid * field_depth_ft)

    return {
        'label':             label,
        'modules':           int(modules),
        'layers':            int(layers),
        'module_plan_area_sf': round(module_plan_area, 3),
        'footprint_sf':      round(footprint_sf, 1),
        'footprint_side_ft': round(fp_side, 1),
        'system_height_ft':  round(system_height_ft, 2),
        'field_depth_ft':    round(field_depth_ft, 2),
        'excavation_cf':     round(excavation_cf, 1),
        'excavation_cy':     round(excavation_cf / 27.0, 1),
        'crate_storage_cf':  round(crate_storage_cf, 1),
        'stone_env_cf':      round(stone_env_cf, 1),
        'stone_storage_cf':  round(stone_storage_cf, 1),
        'stone_backfill_cf': round(stone_env_cf, 1),
        'stone_backfill_cy': round(stone_env_cf / 27.0, 1),
        'wrap_sf':           round(wrap_sf, 1),
        'wrap_sy':           round(wrap_sf / 9.0, 1),
        'total_storage_cf':  round(total_storage_cf, 1),
        'efficiency_pct':    round(efficiency_pct, 1),
    }


def calc_crate_comparison(payload):
    """Compute the AquaCell-vs-competitor comparison from raw inputs.
    Returns a dict with both sized systems, the deltas, and echo of mode."""
    mode = (payload.get('mode') or 'A').upper()
    aqua_in = payload.get('aquacell', {}) or {}
    comp_in = payload.get('competitor', {}) or {}

    def f(d, key, default=0.0):
        try:
            return float(d.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    # Determine required storage volume (the held-constant basis).
    if mode == 'B':
        required_storage_cf = f(payload, 'required_storage_cf')
        if required_storage_cf <= 0:
            return {'error': 'Enter a required storage volume (cf) for Mode B.'}
    else:
        # Mode A: derive target from the competitor system as specified.
        comp_modules = max(0, int(f(comp_in, 'modules', 0)))
        comp_storage_each = f(comp_in, 'storage_per_module_cf')
        if comp_modules <= 0 or comp_storage_each <= 0:
            return {'error': 'Mode A requires competitor module count and per-module storage.'}
        # Target = competitor CRATE storage (module storage only, apples-to-apples
        # sizing driver; each system then adds its own stone credit on top).
        required_storage_cf = comp_modules * comp_storage_each

    # In Mode A the competitor is sized as specified (fixed module count),
    # not re-derived. In Mode B the competitor is sized to the target.
    if mode == 'A':
        comp_fixed = dict(comp_in)
        comp_storage_each = f(comp_in, 'storage_per_module_cf')
        comp_modules = max(0, int(f(comp_in, 'modules', 0)))
        comp_required = comp_modules * comp_storage_each
        comp_res = _cc_size_system(comp_fixed, comp_required)
        # guard: snap-up could add one module; force exact specified count.
        comp_res['modules'] = comp_modules
    else:
        comp_res = _cc_size_system(comp_in, required_storage_cf)

    aqua_res = _cc_size_system(aqua_in, required_storage_cf)

    # Deltas (AquaCell minus competitor). Negative = AquaCell uses less.
    def delta(a, b):
        return round(a - b, 1)
    def pct(a, b):
        return round((a - b) / b * 100.0, 1) if b else 0.0

    deltas = {
        'modules':          {'d': aqua_res['modules'] - comp_res['modules'],
                             'p': pct(aqua_res['modules'], comp_res['modules'])},
        'footprint_sf':     {'d': delta(aqua_res['footprint_sf'], comp_res['footprint_sf']),
                             'p': pct(aqua_res['footprint_sf'], comp_res['footprint_sf'])},
        'system_height_ft': {'d': delta(aqua_res['system_height_ft'], comp_res['system_height_ft']),
                             'p': pct(aqua_res['system_height_ft'], comp_res['system_height_ft'])},
        'excavation_cy':    {'d': delta(aqua_res['excavation_cy'], comp_res['excavation_cy']),
                             'p': pct(aqua_res['excavation_cy'], comp_res['excavation_cy'])},
        'stone_backfill_cy':{'d': delta(aqua_res['stone_backfill_cy'], comp_res['stone_backfill_cy']),
                             'p': pct(aqua_res['stone_backfill_cy'], comp_res['stone_backfill_cy'])},
        'wrap_sy':          {'d': delta(aqua_res['wrap_sy'], comp_res['wrap_sy']),
                             'p': pct(aqua_res['wrap_sy'], comp_res['wrap_sy'])},
        'total_storage_cf': {'d': delta(aqua_res['total_storage_cf'], comp_res['total_storage_cf']),
                             'p': pct(aqua_res['total_storage_cf'], comp_res['total_storage_cf'])},
        'efficiency_pct':   {'d': delta(aqua_res['efficiency_pct'], comp_res['efficiency_pct']),
                             'p': pct(aqua_res['efficiency_pct'], comp_res['efficiency_pct'])},
    }

    return {
        'mode':                mode,
        'required_storage_cf': round(required_storage_cf, 1),
        'aquacell':            aqua_res,
        'competitor':          comp_res,
        'deltas':              deltas,
    }


def build_crate_comparison_pdf(inputs, results, project_name=None):
    """Single-page quantities-only comparison submittal PDF."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'Crate Comparison (BETA) - Value-Engineering Estimate')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 10
    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    mode = results.get('mode', 'A')
    aq = results['aquacell']
    cp = results['competitor']

    basis_rows = [
        ('Comparison Mode',        'A - Specified competitor design' if mode == 'A'
                                    else 'B - Estimated competitor system'),
        ('Required Storage (basis)', f"{results['required_storage_cf']:,.1f} cf"),
        ('AquaCell System',        aq['label']),
        ('Competitor System',      cp['label']),
    ]
    y = _draw_section_kv(c, y, 'BASIS OF COMPARISON', basis_rows, ncols=2)

    # Mode B estimate stamp.
    if mode == 'B':
        bar_h = 16
        c.setFillColor(AMBER)
        c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 8.5)
        c.drawString(LM + 8, y - bar_h + 4,
                     'ESTIMATED - competitor system is a preliminary estimate, NOT a manufacturer-engineered design.')
        y -= (bar_h + 8)

    # ── Comparison table ──
    bar_h = 16
    c.setFillColor(BLUE)
    c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(LM + 8, y - bar_h + 4, 'SIDE-BY-SIDE QUANTITIES (NO COST)')
    y -= bar_h

    rows = [
        ('Modules / chambers needed',   f"{aq['modules']:,}",           f"{cp['modules']:,}",           results['deltas']['modules']),
        ('Footprint (sf)',              f"{aq['footprint_sf']:,.1f}",   f"{cp['footprint_sf']:,.1f}",   results['deltas']['footprint_sf']),
        ('System height (ft)',          f"{aq['system_height_ft']:,.2f}", f"{cp['system_height_ft']:,.2f}", results['deltas']['system_height_ft']),
        ('Excavation volume (cy)',      f"{aq['excavation_cy']:,.1f}",  f"{cp['excavation_cy']:,.1f}",  results['deltas']['excavation_cy']),
        ('Stone backfill (cy)',         f"{aq['stone_backfill_cy']:,.1f}", f"{cp['stone_backfill_cy']:,.1f}", results['deltas']['stone_backfill_cy']),
        ('Geotextile wrap (sy)',        f"{aq['wrap_sy']:,.1f}",        f"{cp['wrap_sy']:,.1f}",        results['deltas']['wrap_sy']),
        ('Total storage provided (cf)', f"{aq['total_storage_cf']:,.1f}", f"{cp['total_storage_cf']:,.1f}", results['deltas']['total_storage_cf']),
        ('System-scale efficiency (%)', f"{aq['efficiency_pct']:,.1f}%", f"{cp['efficiency_pct']:,.1f}%", results['deltas']['efficiency_pct']),
    ]

    col_metric = LM + 8
    col_aq     = LM + 250
    col_cp     = LM + 350
    col_delta  = LM + 450
    row_h = 18
    header_h = 15

    c.setFillColor(MGRAY)
    c.rect(LM, y - header_h, CW, header_h, fill=1, stroke=0)
    c.setFillColor(BLACK)
    c.setFont('Helvetica-Bold', 7.5)
    c.drawString(col_metric, y - header_h + 4, 'METRIC')
    c.drawString(col_aq, y - header_h + 4, 'AQUACELL')
    c.drawString(col_cp, y - header_h + 4, 'COMPETITOR')
    c.drawString(col_delta, y - header_h + 4, 'DELTA (AC - COMP)')
    y -= header_h

    for i, (metric, aqv, cpv, dl) in enumerate(rows):
        rh = row_h
        c.setFillColor(LGRAY if i % 2 == 0 else WHITE)
        c.setStrokeColor(MGRAY)
        c.setLineWidth(0.5)
        c.rect(LM, y - rh, CW, rh, fill=1, stroke=1)
        c.setFillColor(BLACK)
        c.setFont('Helvetica', 8)
        c.drawString(col_metric, y - rh + 5, metric)
        c.setFont('Helvetica-Bold', 8)
        c.drawString(col_aq, y - rh + 5, aqv)
        c.drawString(col_cp, y - rh + 5, cpv)
        d_val = dl.get('d', 0)
        d_pct = dl.get('p', 0)
        higher_is_better = metric.startswith('System-scale efficiency') or metric.startswith('Total storage')
        if d_val == 0:
            c.setFillColor(GRAY)
        elif (d_val < 0) != higher_is_better:
            c.setFillColor(GREEN)
        else:
            c.setFillColor(RED)
        sign = '+' if d_val > 0 else ''
        c.drawString(col_delta, y - rh + 5, f"{sign}{d_val:,.1f} ({sign}{d_pct:,.1f}%)")
        y -= rh

    y -= 10

    note_lines = [
        'BASIS & DISCLAIMER',
        'Both systems sized to an equal required storage volume, each by its own rules (own per-module storage,',
        'own stone envelope: base / cover / perimeter / stone void). System-scale efficiency = total storage /',
        'total excavated field, computed identically for both systems; it is this tool\'s figure and differs from a',
        'vendor\'s single-module sheet value. Competitor values are USER-SUPPLIED. This is an internal',
        'value-engineering estimate for reference only - not a manufacturer-engineered design or a published',
        'competitive claim. Quantities only; no cost figures are implied.',
    ]
    c.setFillColor(GRAY)
    for i, ln in enumerate(note_lines):
        if i == 0:
            c.setFont('Helvetica-Bold', 8)
            c.setFillColor(BLACK)
        else:
            c.setFont('Helvetica', 7)
            c.setFillColor(GRAY)
        c.drawString(LM, y, ln)
        y -= 10

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


# ══════════════════════════════════════════════════════════════════
#  SITE OVERLAY (BETA)  —  conceptual tank footprint over a plan page
#  Receives a client-composited JPEG (base64) and drops it onto a
#  letter page with header + disclaimer. No calc engine: the picture
#  is built in the browser; the server only wraps it as a submittal.
# ══════════════════════════════════════════════════════════════════
_SITE_OVERLAY_DISCLAIMER_TEXT = (
    "This site overlay is a CONCEPTUAL VISUAL AID only and is NOT TO SCALE FOR MEASUREMENT. "
    "The tank footprint shown is positioned by hand over a raster (image) copy of the plan; it is "
    "NOT a surveyed or CAD-accurate placement. Any dimension, area, or setback read from this "
    "overlay is approximate and MUST NOT be used for construction, permitting, or takeoff. Refer to "
    "the stamped plans and a project-specific measurement (e.g. CAD / Bluebeam) as the source of "
    "truth for all dimensions and quantities. AquaCell module dimensions follow published product "
    "data. FINAL LAYOUTS, CAPACITIES, AND INSTALLATION DEPTHS MUST BE CONFIRMED BY A LICENSED "
    "PROFESSIONAL ENGINEER."
)
_SITE_OVERLAY_BOLD_TRIGGERS = ['NOT TO SCALE FOR MEASUREMENT', 'NOT', 'MUST NOT', 'source of truth',
                               'MUST BE CONFIRMED BY A LICENSED PROFESSIONAL ENGINEER']


def build_site_overlay_pdf(image_data_url, project_name=None, meta=None):
    """One-page conceptual submittal PDF wrapping a client-composited
    site overlay image. image_data_url is a 'data:image/jpeg;base64,...'
    string produced by the browser canvas. meta is an optional dict with
    keys like tank_wl, config, scale_note, view_mode for a caption line.
    Returns a BytesIO buffer positioned at 0."""
    import base64 as _b64
    meta = meta or {}
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    # ── header band (matches other submittal PDFs) ──
    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'Site Overlay \u2014 Conceptual Submittal')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)
    y = PH - band_h - 10

    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 15

    # ── caption line (config / tank W x L / view mode) ──
    cap_bits = []
    if meta.get('config'):
        cap_bits.append(f"AquaCell {meta['config']}")
    if meta.get('tank_wl'):
        cap_bits.append(str(meta['tank_wl']))
    if meta.get('view_mode'):
        cap_bits.append(str(meta['view_mode']))
    if cap_bits:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 10)
        c.drawString(LM, y, '  |  '.join(cap_bits))
        y -= 6

    # ── title bar over the image ──
    bar_h = 16
    c.setFillColor(BLUE)
    c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(LM + 8, y - bar_h + 4,
                 'CONCEPTUAL SITE OVERLAY \u2014 SHADED = AQUACELL FOOTPRINT (NOT TO SCALE)')
    box_top = y - bar_h

    # ── image box, sized to leave room for the disclaimer ──
    disc_h = _disclaimer_box_height(_SITE_OVERLAY_DISCLAIMER_TEXT)
    box_bottom = 40 + disc_h + 10
    box_h = box_top - box_bottom
    c.setFillColor(WHITE)
    c.setStrokeColor(MGRAY)
    c.setLineWidth(0.5)
    c.rect(LM, box_bottom, CW, box_h, fill=1, stroke=1)

    # ── decode and draw the composited overlay, letterboxed to fit ──
    try:
        raw = image_data_url
        if ',' in raw:
            raw = raw.split(',', 1)[1]
        img_bytes = _b64.b64decode(raw)
        img_reader = ImageReader(io.BytesIO(img_bytes))
        iw, ih = img_reader.getSize()
        avail_w = CW - 12
        avail_h = box_h - 12
        if iw > 0 and ih > 0 and avail_w > 0 and avail_h > 0:
            scale = min(avail_w / iw, avail_h / ih)
            draw_w = iw * scale
            draw_h = ih * scale
            draw_x = LM + 6 + (avail_w - draw_w) / 2.0
            draw_y = box_bottom + 6 + (avail_h - draw_h) / 2.0
            c.drawImage(img_reader, draw_x, draw_y, width=draw_w, height=draw_h,
                        preserveAspectRatio=True, mask=None)
    except Exception as exc:
        c.setFillColor(colors.HexColor('#b91c1c'))
        c.setFont('Helvetica-Bold', 10)
        c.drawString(LM + 12, box_bottom + box_h / 2.0,
                     f'Overlay image could not be embedded: {exc}')

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_SITE_OVERLAY_DISCLAIMER_TEXT,
                            bold_triggers=_SITE_OVERLAY_BOLD_TRIGGERS)
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


# ══════════════════════════════════════════════════════════════════
#  DESIGN VERIFICATION DASHBOARD — PT-ROW™ Transparent Sizing Calculator
#
#  Shows the engineer four independent sizing methods side by side rather
#  than a single black-box number, per James's explicit direction: no
#  auto-jurisdiction toggle, no method forced as "controlling" — the
#  engineer picks which governs. Each method computes only from the
#  inputs it needs; missing inputs mark that method unavailable rather
#  than blocking the whole calculation, since a user may only have a
#  flow figure or only a volume figure on hand.
#
#  v1 geometry (Filter Area-Based method + fabric takeoff) is Offset
#  (Side-Car) layout only — AQ-400-01 Sheets 4-8 for Header Row/Inline/
#  3-Stack have not been provided. Do not fabricate geometry for those;
#  see spec Section 7/8.
# ══════════════════════════════════════════════════════════════════
_PT_ROW_FLOW_COEFF = 0.464   # CFS per crate — Wavin PT-ROW™ Sizing Guidance
_PT_ROW_NET_STORAGE_CF = {   # ft3 per module, flat (not layer-dependent)
    # SC: AQ-200-01 Rev 4 "AquaCell Crate With Bottom Plate (1st Layer Only)," SC-1 row.
    # EX: AQ-200-01 Rev 4 "AquaCell Crate + (2 Base Units)," EX-1 row (same no-side-plate,
    # single-layer basis as SC).
    'SC': 10.25,
    'EX': 10.83,
}
_PT_ROW_WOVEN_LAYERS = 2     # AQ-400-01 §1.1 — two layers of woven geotextile wrap the forebay

_PT_ROW_REFERENCE_NOTES = [
    'Woven geotextile only within the PT-ROW forebay wrap — non-woven is not permitted here '
    '(non-woven wraps the main storage system only).',
    'Two layers of woven geotextile required: first layer under the row over supporting stone, '
    'second layer fully encloses the row.',
    'Minimum access structures: one at inlet/diversion, one at downstream end; additional access '
    'required for rows exceeding 300 ft. Minimum 12" diameter openings.',
    'Aggregate must be non-angular and clean, free of fines, to prevent fabric damage.',
    'Final row length, width, and height are determined by the Engineer of Record — this tool '
    'supports sizing, it does not replace EOR judgment.',
]

_PT_ROW_DISCLAIMER_TEXT = (
    "DISCLAIMER: This tool shows four independent PT-ROW pretreatment sizing methods side by side "
    "so the Engineer of Record can see the math behind each and select which governs for their "
    "jurisdiction — no method is auto-selected as controlling. It does not replace the embedded "
    "PT-ROW logic in the Single/Multi-Tank calculators, verify the governing method's basis and "
    "applicable code reference before use. Filter Area-Based sizing and fabric takeoff geometry "
    "shown here are Offset (Side-Car) layout only; Header Row, Inline, and 3-Stack layouts are not "
    "yet supported. THE ENGINEER OF RECORD IS SOLELY RESPONSIBLE FOR SELECTING THE GOVERNING SIZING "
    "METHOD AND VERIFYING FINAL ROW LENGTH, WIDTH, AND HEIGHT against project-specific hydrology, "
    "jurisdictional requirements, and AQ-400-01."
)
_PT_ROW_BOLD_TRIGGERS = ('THE ENGINEER OF RECORD',)


def calc_pt_row(payload):
    config       = payload.get('config', 'SC')
    if config not in ('SC', 'EX'):
        config = 'SC'
    layers       = int(float(payload.get('layers', 1) or 1))
    layout       = payload.get('layout', 'offset')
    if layout not in ('offset', 'header_row', 'inline', '3_stack'):
        layout = 'offset'

    wqf_cfs           = float(payload.get('wqf_cfs', 0) or 0)
    required_volume_cf = float(payload.get('required_volume_cf', 0) or 0)
    loading_rate       = float(payload.get('loading_rate_gpm_sf', 1.0) or 1.0)
    crates_per_row      = int(float(payload.get('crates_per_row', 0) or 0))
    num_rows            = int(float(payload.get('num_rows', 0) or 0))
    wrap_ext_ft          = float(payload.get('wrap_extension_ft', 1.5) or 1.5)
    waste_factor         = float(payload.get('waste_factor', 0.20) or 0.20)

    layer_heights = CONFIG_DATA[config]['layer_heights']
    layers = max(1, min(layers, len(layer_heights)))
    layer_height_ft = layer_heights[layers - 1]

    crate_width_ft  = MODULE_WID
    crate_length_ft = MODULE_LEN

    # ── 3.1 Flow-Based (Legacy Wavin) ───────────────────────────────
    flow_based = {'available': False, 'crates_required': None,
                  'formula': 'CEILING(WQf ÷ 0.464, 1)',
                  'basis': 'Wavin PT-ROW™ Sizing Guidance — 0.464 CFS/crate flow coefficient '
                           '(same constant used by the embedded Single/Multi-Tank PT-ROW logic).',
                  'note': None}
    if wqf_cfs > 0:
        flow_based['available'] = True
        flow_based['crates_required'] = math.ceil(wqf_cfs / _PT_ROW_FLOW_COEFF)
    else:
        flow_based['note'] = 'Enter Treatment Flow (WQf) to compute.'

    # ── 3.2 Volume-Based ─────────────────────────────────────────────
    net_storage_cf = _PT_ROW_NET_STORAGE_CF[config]
    volume_based = {'available': False, 'crates_required': None,
                     'net_storage_per_module_cf': net_storage_cf,
                     'formula': 'CEILING(Required Volume ÷ net storage per module, 1)',
                     'basis': f"AQ-200-01 Rev 4 — {config}-1 water volume at 1 layer, base "
                              f"configuration, no side plates ({net_storage_cf} ft³/module).",
                     'note': None}
    if required_volume_cf > 0:
        volume_based['available'] = True
        volume_based['crates_required'] = math.ceil(required_volume_cf / net_storage_cf)
    else:
        volume_based['note'] = 'Enter Required Pretreatment Volume to compute.'

    # ── Shared: required filter area (methods 3.3 and 3.4) ─────────
    filter_area_ok = wqf_cfs > 0 and loading_rate > 0
    required_filter_area_ft2 = (wqf_cfs * 448.8 / loading_rate) if filter_area_ok else None

    # ── 3.3 Filter Area-Based (Geometric Method) — Offset layout only, v1 ──
    filter_area_based = {'available': False, 'required_filter_area_ft2': None,
                          'effective_filter_area_per_crate_ft2': None,
                          'crates_required_estimate': None,
                          'provided_filter_area_ft2': None, 'area_margin_ft2': None, 'status': None,
                          'formula': 'Required area = (WQf × 448.8) ÷ Loading Rate; '
                                     'crates = CEILING(required area ÷ area per crate, 1)',
                          'basis': 'AQ-400-01 §3.1–3.3 — filter area sized to pass WQf at the '
                                   'selected loading rate (default 1.0 gpm/ft²); all woven fabric '
                                   'surfaces in contact with aggregate count (bottom + two long '
                                   'sidewalls). Offset (Side-Car) geometry only.',
                          'note': None}
    if layout != 'offset':
        filter_area_based['note'] = 'Filter Area-Based sizing is only available for the Offset (Side-Car) layout in v1.'
    elif not filter_area_ok:
        filter_area_based['note'] = 'Enter Treatment Flow (WQf) and Loading Rate to compute.'
    else:
        effective_filter_width_ft = crate_width_ft + 2 * layer_height_ft
        effective_filter_area_per_crate = crate_length_ft * effective_filter_width_ft
        filter_area_based['available'] = True
        filter_area_based['required_filter_area_ft2'] = round(required_filter_area_ft2, 1)
        filter_area_based['effective_filter_area_per_crate_ft2'] = round(effective_filter_area_per_crate, 2)
        filter_area_based['crates_required_estimate'] = math.ceil(required_filter_area_ft2 / effective_filter_area_per_crate)
        if crates_per_row > 0 and num_rows > 0:
            provided = crates_per_row * num_rows * effective_filter_area_per_crate
            filter_area_based['provided_filter_area_ft2'] = round(provided, 1)
            filter_area_based['area_margin_ft2'] = round(provided - required_filter_area_ft2, 1)
            filter_area_based['status'] = 'PASS' if provided >= required_filter_area_ft2 else 'CHECK'
        else:
            filter_area_based['note'] = 'Enter Selected Crates per PT Row and # of PT Rows to check provided area against required.'

    # ── 3.4 Quick Estimate (Flat Area Method) — legacy rough estimate ──
    quick_estimate = {'available': False, 'required_filter_area_ft2': None, 'crates_estimate': None,
                       'formula': 'CEILING(((WQf × 448.8) ÷ Loading Rate) ÷ 20, 1)',
                       'basis': 'Legacy rough estimate — flat 20 sf/module approximation, not '
                                'derived from actual AquaCell crate geometry.',
                       'note': 'Rough estimate only — does not account for actual crate filter '
                               'surface geometry. Use Filter Area-Based (Geometric Method) for a '
                               'verified result.'}
    if filter_area_ok:
        quick_estimate['available'] = True
        quick_estimate['required_filter_area_ft2'] = round(required_filter_area_ft2, 1)
        quick_estimate['crates_estimate'] = math.ceil(required_filter_area_ft2 / 20.0)
    else:
        quick_estimate['note'] = 'Enter Treatment Flow (WQf) and Loading Rate to compute. ' + quick_estimate['note']

    # ── Section 4: Fabric Takeoff — Offset layout only, v1 ──────────
    fabric_takeoff = {'available': False, 'layout': layout,
                       'fabric_width_per_row_ft': None, 'fabric_length_per_row_ft': None,
                       'fabric_per_row_per_layer_sqyd': None, 'woven_layers': _PT_ROW_WOVEN_LAYERS,
                       'total_woven_fabric_sqyd': None, 'total_woven_fabric_rounded_sqyd': None,
                       'note': None}
    if layout != 'offset':
        fabric_takeoff['note'] = 'Fabric takeoff not yet available for this configuration — drawing detail pending.'
    elif crates_per_row <= 0 or num_rows <= 0:
        fabric_takeoff['note'] = 'Enter Selected Crates per PT Row and # of PT Rows to compute fabric takeoff.'
    else:
        fabric_width_ft = crate_width_ft + 2 * layer_height_ft + 2 * wrap_ext_ft
        fabric_length_ft = crates_per_row * crate_length_ft + 2 * wrap_ext_ft
        fabric_per_row_per_layer_sqyd = (fabric_width_ft * fabric_length_ft * (1 + waste_factor)) / 9.0
        total_sqyd = fabric_per_row_per_layer_sqyd * num_rows * _PT_ROW_WOVEN_LAYERS
        fabric_takeoff['available'] = True
        fabric_takeoff['fabric_width_per_row_ft'] = round(fabric_width_ft, 2)
        fabric_takeoff['fabric_length_per_row_ft'] = round(fabric_length_ft, 2)
        fabric_takeoff['fabric_per_row_per_layer_sqyd'] = round(fabric_per_row_per_layer_sqyd, 1)
        fabric_takeoff['total_woven_fabric_sqyd'] = round(total_sqyd, 1)
        fabric_takeoff['total_woven_fabric_rounded_sqyd'] = math.ceil(total_sqyd)

    return {
        'config': config,
        'layers': layers,
        'layer_height_ft': round(layer_height_ft, 3),
        'layout': layout,
        'crates_per_row': crates_per_row,
        'num_rows': num_rows,
        'wrap_extension_ft': round(wrap_ext_ft, 2),
        'waste_factor': round(waste_factor, 2),
        'flow_based': flow_based,
        'volume_based': volume_based,
        'filter_area_based': filter_area_based,
        'quick_estimate': quick_estimate,
        'fabric_takeoff': fabric_takeoff,
        'reference_notes': _PT_ROW_REFERENCE_NOTES,
    }


_PT_ROW_LAYOUT_LABELS = {
    'offset': 'Offset (Side-Car)',
    'header_row': 'Header Row',
    'inline': 'Inline',
    '3_stack': '3-Stack',
}


def build_pt_row_pdf(inputs, results, project_name=None):
    """Single-page submittal-ready PDF for the PT-ROW Transparent Sizing Calculator."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'PT-ROW™ Transparent Sizing Calculator')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 10
    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    input_rows = [
        ('Configuration', 'SC — Standard' if results['config'] == 'SC' else 'EX — Extra Strong'),
        ('PT-ROW Layout', _PT_ROW_LAYOUT_LABELS.get(results['layout'], results['layout'])),
        ('Number of Layers', f"{results['layers']} ({results['layer_height_ft']} ft)"),
        ('Treatment Flow (WQf)', f"{inputs.get('wqf_cfs', 0)} cfs"),
        ('Required Pretreatment Volume', f"{inputs.get('required_volume_cf', 0)} ft³"),
        ('Loading Rate', f"{inputs.get('loading_rate_gpm_sf', 1.0)} gpm/ft²"),
        ('Selected Crates per PT Row', str(results['crates_per_row'] or '—')),
        ('Selected # of PT Rows', str(results['num_rows'] or '—')),
    ]
    y = _draw_section_kv(c, y, 'INPUTS', input_rows, ncols=2)

    def _m_val(m, key='crates_required', unit=' crates'):
        if not m['available']:
            return 'N/A'
        return f"{m[key]}{unit}"

    fab = results['filter_area_based']
    fab_val = 'N/A'
    if fab['available']:
        fab_val = f"{fab['crates_required_estimate']} crates est."
        if fab['status']:
            fab_val += f" — {fab['provided_filter_area_ft2']:,.0f} sf provided vs {fab['required_filter_area_ft2']:,.0f} sf req. ({fab['status']})"

    qe = results['quick_estimate']
    qe_val = f"{qe['crates_estimate']} crates (rough est.)" if qe['available'] else 'N/A'

    method_rows = [
        ('Flow-Based (Legacy Wavin)', _m_val(results['flow_based'])),
        ('Volume-Based', _m_val(results['volume_based'])),
        ('Filter Area-Based (Geometric Method)', fab_val),
        ('Quick Estimate (Flat Area Method)', qe_val),
    ]
    y = _draw_section_kv(c, y, 'SIZING METHODS — ENGINEER SELECTS GOVERNING METHOD', method_rows, ncols=1)

    ft = results['fabric_takeoff']
    if ft['available']:
        fabric_rows = [
            ('Fabric Width per Row', f"{ft['fabric_width_per_row_ft']} ft"),
            ('Fabric Length per Row', f"{ft['fabric_length_per_row_ft']} ft"),
            ('Woven Layers', str(ft['woven_layers'])),
            ('Total Woven Fabric', f"{ft['total_woven_fabric_rounded_sqyd']} sy (raw {ft['total_woven_fabric_sqyd']} sy)"),
        ]
        y = _draw_section_kv(c, y, 'FABRIC TAKEOFF (OFFSET / SIDE-CAR)', fabric_rows, ncols=2)

    # ── Condensed reference notes (bulleted, wrapped) ───────────────
    from reportlab.pdfbase.pdfmetrics import stringWidth
    bar_h = 16
    c.setFillColor(BLUE)
    c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(LM + 8, y - bar_h + 4, 'CONDENSED REFERENCE NOTES (SEE AQ-400-01 FOR FULL SPEC)')
    notes_top = y - bar_h
    line_h = 10
    pad = 8
    max_w = CW - pad * 2 - 10
    wrapped = []
    for note_text in results['reference_notes']:
        bullet = '• ' + note_text
        words = bullet.split()
        cur = ''
        for wd in words:
            test = (cur + ' ' + wd).strip()
            if stringWidth(test, 'Helvetica', 7) <= max_w:
                cur = test
            else:
                if cur:
                    wrapped.append(cur)
                cur = wd
        if cur:
            wrapped.append(cur)
    notes_h = len(wrapped) * line_h + pad * 2
    notes_bottom = notes_top - notes_h
    c.setFillColor(LGRAY)
    c.setStrokeColor(MGRAY)
    c.setLineWidth(0.5)
    c.rect(LM, notes_bottom, CW, notes_h, fill=1, stroke=1)
    ty = notes_top - pad - 6
    c.setFont('Helvetica', 7)
    c.setFillColor(BLACK)
    for line in wrapped:
        c.drawString(LM + pad, ty, line)
        ty -= line_h
    y = notes_bottom - 10

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_PT_ROW_DISCLAIMER_TEXT,
                            bold_triggers=_PT_ROW_BOLD_TRIGGERS)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


# ══════════════════════════════════════════════════════════════════
#  PDF BUILDER — Stage-Storage Calculator
#
#  Stage-Storage is 100% client-side by design (no backend dependency —
#  see the original standalone-tool spec). Unlike every other Design
#  Tools PDF, this one does NOT recompute from raw inputs server-side:
#  the browser sends its already-computed result object (ssLastCalc)
#  directly and this just formats it. The full elevation-by-elevation
#  table stays on the Export CSV button — this sheet is summary-only,
#  matching the one-page density of every other submittal PDF here.
# ══════════════════════════════════════════════════════════════════
_STAGE_STORAGE_DISCLAIMER_TEXT = (
    "DISCLAIMER: This calculator provides preliminary, conceptual estimates only and is not a "
    "stamped engineering design. Base, cover, and perimeter stone depths/widths, void ratios, and "
    "tank geometry are all independently adjustable inputs — verify every value against "
    "project-specific plans. The full elevation-by-elevation stage-storage table is available via "
    "the Export CSV button in the dashboard, not reproduced on this sheet. THE ENGINEER OF RECORD "
    "IS SOLELY RESPONSIBLE FOR CONFIRMING ALL DESIGN PARAMETERS, SITE CONDITIONS, AND FINAL "
    "STORAGE VOLUMES."
)
_STAGE_STORAGE_BOLD_TRIGGERS = ('THE ENGINEER OF RECORD',)


def build_stage_storage_pdf(calc, project_name=None):
    """Single-page submittal-ready PDF for the Stage-Storage Calculator.

    `calc` is the client's already-computed ssLastCalc object (inputs +
    results), not raw form inputs — see module docstring above.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    generated_str = datetime.datetime.now().strftime('%m/%d/%Y %I:%M %p')

    band_h = 64
    c.setFillColor(NAVY)
    c.rect(0, PH - band_h, PW, band_h, fill=1, stroke=0)
    logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            c.drawImage(img, LM, PH - band_h + 8, width=160, height=46,
                        preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 14)
    c.drawRightString(PW - RM, PH - 24, 'AquaCell® Design Verification Dashboard')
    c.setFont('Helvetica', 9)
    c.setFillColor(colors.HexColor('#93c5fd'))
    c.drawRightString(PW - RM, PH - 37, 'Stage-Storage Calculator')
    c.setFont('Helvetica', 7)
    c.setFillColor(colors.HexColor('#94a3b8'))
    c.drawRightString(PW - RM, PH - 50, generated_str)

    y = PH - band_h - 10
    if project_name:
        c.setFillColor(GRAY)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(LM, y, f'Project: {project_name}')
        y -= 16

    inputs = calc.get('inputs', {}) or {}
    cfg = inputs.get('cfg', 'SC')
    footprint_mode = inputs.get('footprintMode', 'rect')

    if footprint_mode == 'rect':
        footprint_str = (
            f"{float(inputs.get('tankWidth', 0) or 0):.3f} x {float(inputs.get('tankLength', 0) or 0):.3f} ft "
            f"(snapped from {inputs.get('knownWidth', 0)} x {inputs.get('knownLength', 0)} ft)"
        )
    else:
        geo = calc.get('complexGeo', {}) or {}
        footprint_str = (
            f"{float(geo.get('snappedKnown', 0) or 0):.3f} x {float(geo.get('snappedOther', 0) or 0):.3f} ft "
            f"(Complex Shape method)"
        )

    geom_rows = [
        ('Configuration', 'SC — Standard' if cfg == 'SC' else 'EX — Extra Strong'),
        ('Layers', str(inputs.get('layers', ''))),
        ('AquaCell Footprint', footprint_str),
        ('Tank Height', f"{inputs.get('tankHeight', 0)} ft"),
        ('Tank Bottom Elevation', f"{inputs.get('tankBottomElev', 0)} ft"),
        ('Base Stone', f"{inputs.get('baseDepth', 0)} ft" + ('' if inputs.get('baseIncl') else ' (excluded)')),
        ('Cover Stone', f"{inputs.get('coverDepth', 0)} ft" + ('' if inputs.get('coverIncl') else ' (excluded)')),
        ('Perimeter Stone', f"{inputs.get('perimWidth', 0)} ft" + ('' if inputs.get('perimIncl') else ' (excluded)')),
        ('Stone Void Ratio', str(inputs.get('stoneVoid', ''))),
        ('Stage Table Increment', f"{inputs.get('stageIncrementIn', 0)} in"),
    ]
    y = _draw_section_kv(c, y, 'TANK / CRATE GEOMETRY', geom_rows, ncols=2)

    def _n(key):
        return float(calc.get(key, 0) or 0)

    result_rows = [
        ('Tank Storage', f"{_n('tankStorageTotal'):,.1f} ft³"),
        ('Base Stone Storage', f"{_n('baseStoneNet'):,.1f} ft³"),
        ('Cover Stone Storage', f"{_n('coverStoneNet'):,.1f} ft³"),
        ('Perimeter Stone Storage', f"{_n('perimStoneNet'):,.1f} ft³"),
        ('Total Stone Storage', f"{_n('stoneStorageTotal'):,.1f} ft³"),
        ('Grand Total Storage', f"{_n('grandTotalStorage'):,.1f} ft³"),
        ('Total System Depth', f"{_n('totalSystemDepth'):,.2f} ft"),
        ('Bottom of Base Stone Elev.', f"{_n('elevBaseStart'):,.2f} ft"),
        ('Top of Tank Elev.', f"{_n('elevTankTop'):,.2f} ft"),
        ('Top of Stone Elev.', f"{_n('elevCoverTop'):,.2f} ft"),
    ]
    y = _draw_section_kv(c, y, 'RESULTS', result_rows, ncols=2)

    note = (
        f"Full elevation-by-elevation stage-storage table (every {inputs.get('stageIncrementIn', 3)} in) "
        "is available via the Export CSV button in the dashboard — not reproduced here to keep this "
        "submittal sheet to one page."
    )
    from reportlab.pdfbase.pdfmetrics import stringWidth
    bar_h = 16
    c.setFillColor(BLUE)
    c.rect(LM, y - bar_h, CW, bar_h, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(LM + 8, y - bar_h + 4, 'STAGE-STORAGE TABLE')
    note_top = y - bar_h
    line_h = 10
    pad = 8
    max_w = CW - pad * 2
    words = note.split()
    lines = []
    cur = ''
    for wd in words:
        test = (cur + ' ' + wd).strip()
        if stringWidth(test, 'Helvetica', 7.5) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    note_h = len(lines) * line_h + pad * 2
    note_bottom = note_top - note_h
    c.setFillColor(LGRAY)
    c.setStrokeColor(MGRAY)
    c.setLineWidth(0.5)
    c.rect(LM, note_bottom, CW, note_h, fill=1, stroke=1)
    ty = note_top - pad - 6
    c.setFont('Helvetica', 7.5)
    c.setFillColor(BLACK)
    for line in lines:
        c.drawString(LM + pad, ty, line)
        ty -= line_h
    y = note_bottom - 10

    _draw_disclaimer_block(c, 40, disclaimer_lines_raw=_STAGE_STORAGE_DISCLAIMER_TEXT,
                            bold_triggers=_STAGE_STORAGE_BOLD_TRIGGERS)

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


# ══════════════════════════════════════════════════════════════════
#  ROUTE: /design-tools  —  Design Verification Dashboard (page)
# ══════════════════════════════════════════════════════════════════
@app.route('/design-tools', methods=['GET'])
def design_tools_index():
    return render_template('design_tools.html')


# ══════════════════════════════════════════════════════════════════
#  ROUTE: /design-tools/calculate  —  JSON API called by JS.
#  Single dispatcher endpoint so future calculators (loading, min
#  cover, manhole distance, etc.) can be added without new routes —
#  just a new calc_type branch and a new calc_* function.
# ══════════════════════════════════════════════════════════════════
@app.route('/design-tools/calculate', methods=['POST'])
def design_tools_calculate():
    try:
        data      = request.get_json(force=True)
        calc_type = data.get('calc_type', '')
        payload   = data.get('inputs', {})

        if calc_type == 'buoyancy':
            result = calc_buoyancy(payload)
        elif calc_type == 'loading_truck':
            traffic_load  = payload.get('traffic_load', 'HS20')
            cover_ft      = float(payload.get('cover_ft', 0) or 0)
            config        = payload.get('config', 'SC')
            fill_pcf      = float(payload.get('fill_pcf', _LL_FILL_PCF_DEFAULT) or _LL_FILL_PCF_DEFAULT)
            if cover_ft <= 0:
                return jsonify({'error': 'Enter a valid cover depth.'}), 400
            result = calc_astm_f2787_truck(traffic_load, cover_ft, config, fill_pcf)
            if result is None:
                return jsonify({'error': 'Enter a valid cover depth.'}), 400
        elif calc_type == 'loading_outrigger':
            total_weight_lbs = float(payload.get('total_weight_lbs', 0) or 0)
            pad_shape        = payload.get('pad_shape', 'Rectangular')
            pad_length_in    = float(payload.get('pad_length_in', 0) or 0)
            pad_width_in     = float(payload.get('pad_width_in', 0) or 0)
            pad_diameter_in  = float(payload.get('pad_diameter_in', 0) or 0)
            cover_ft         = float(payload.get('cover_ft', 0) or 0)
            fill_pcf         = float(payload.get('fill_pcf', _LL_FILL_PCF_DEFAULT) or _LL_FILL_PCF_DEFAULT)
            load_factor_pct  = float(payload.get('load_factor_pct', 75.0) or 75.0)
            config           = payload.get('config', 'SC')
            result = calc_outrigger_load(total_weight_lbs, pad_shape, pad_length_in, pad_width_in,
                                          pad_diameter_in, cover_ft, fill_pcf, load_factor_pct, config)
        elif calc_type == 'excavation_slope':
            result = calc_excavation_slope(payload)
        elif calc_type == 'stepped_backfill':
            result = calc_stepped_backfill(payload)
        elif calc_type == 'min_cover_burial':
            result = calc_min_cover_burial(payload)
        elif calc_type == 'min_distance_structure':
            result = calc_min_distance_structure(payload)
        elif calc_type == 'complex_shape_builder':
            result = calc_complex_shape_builder(payload)
        elif calc_type == 'pt_row':
            result = calc_pt_row(payload)
        else:
            return jsonify({'error': f'Unknown calc_type: {calc_type}'}), 400

        if 'error' in result:
            return jsonify(result), 400

        return jsonify(result)
    except Exception as exc:
        import traceback
        return jsonify({'error': str(exc), 'trace': traceback.format_exc()}), 500


# ══════════════════════════════════════════════════════════════════
#  ROUTE: /design-tools/download_pdf  —  single-page submittal PDF
# ══════════════════════════════════════════════════════════════════
@app.route('/design-tools/download_pdf', methods=['POST'])
def design_tools_download_pdf():
    try:
        data      = request.get_json(force=True)
        calc_type = data.get('calc_type', '')
        payload   = data.get('inputs', {})
        project_name = data.get('project_name') or None

        if calc_type == 'buoyancy':
            result = calc_buoyancy(payload)
            if 'error' in result:
                return jsonify(result), 400
            buffer = build_buoyancy_pdf(payload, result, project_name=project_name)
            download_name = 'AquaCell_Buoyancy_Check.pdf'
        elif calc_type == 'loading_truck':
            traffic_load = payload.get('traffic_load', 'HS20')
            cover_ft     = float(payload.get('cover_ft', 0) or 0)
            config       = payload.get('config', 'SC')
            fill_pcf     = float(payload.get('fill_pcf', _LL_FILL_PCF_DEFAULT) or _LL_FILL_PCF_DEFAULT)
            if cover_ft <= 0:
                return jsonify({'error': 'Enter a valid cover depth.'}), 400
            result = calc_astm_f2787_truck(traffic_load, cover_ft, config, fill_pcf)
            if result is None:
                return jsonify({'error': 'Enter a valid cover depth.'}), 400
            buffer = build_loading_truck_pdf(payload, result, project_name=project_name)
            download_name = f"AquaCell_Loading_Truck_{result['traffic_load']}.pdf"
        elif calc_type == 'loading_outrigger':
            total_weight_lbs = float(payload.get('total_weight_lbs', 0) or 0)
            pad_shape        = payload.get('pad_shape', 'Rectangular')
            pad_length_in    = float(payload.get('pad_length_in', 0) or 0)
            pad_width_in     = float(payload.get('pad_width_in', 0) or 0)
            pad_diameter_in  = float(payload.get('pad_diameter_in', 0) or 0)
            cover_ft         = float(payload.get('cover_ft', 0) or 0)
            fill_pcf         = float(payload.get('fill_pcf', _LL_FILL_PCF_DEFAULT) or _LL_FILL_PCF_DEFAULT)
            load_factor_pct  = float(payload.get('load_factor_pct', 75.0) or 75.0)
            config           = payload.get('config', 'SC')
            result = calc_outrigger_load(total_weight_lbs, pad_shape, pad_length_in, pad_width_in,
                                          pad_diameter_in, cover_ft, fill_pcf, load_factor_pct, config)
            if 'error' in result:
                return jsonify(result), 400
            buffer = build_loading_outrigger_pdf(payload, result, project_name=project_name)
            download_name = 'AquaCell_Loading_Outrigger.pdf'
        elif calc_type == 'excavation_slope':
            result = calc_excavation_slope(payload)
            if 'error' in result:
                return jsonify(result), 400
            buffer = build_excavation_slope_pdf(payload, result, project_name=project_name)
            download_name = 'AquaCell_Excavation_Slope.pdf'
        elif calc_type == 'stepped_backfill':
            result = calc_stepped_backfill(payload)
            if 'error' in result:
                return jsonify(result), 400
            buffer = build_stepped_backfill_pdf(payload, result, project_name=project_name)
            download_name = 'AquaCell_Stepped_Backfill.pdf'
        elif calc_type == 'min_cover_burial':
            result = calc_min_cover_burial(payload)
            if 'error' in result:
                return jsonify(result), 400
            buffer = build_min_cover_burial_pdf(payload, result, project_name=project_name)
            download_name = 'AquaCell_Min_Cover_Burial_Depth.pdf'
        elif calc_type == 'min_distance_structure':
            result = calc_min_distance_structure(payload)
            if 'error' in result:
                return jsonify(result), 400
            buffer = build_min_dist_struct_pdf(payload, result, project_name=project_name)
            download_name = 'AquaCell_Min_Distance_from_Structure.pdf'
        elif calc_type == 'complex_shape_builder':
            result = calc_complex_shape_builder(payload)
            if 'error' in result:
                return jsonify(result), 400
            buffer = build_complex_shape_pdf(payload, result, project_name=project_name)
            download_name = 'AquaCell_Complex_Shape_Schematic.pdf'
        elif calc_type == 'crate_comparison':
            result = calc_crate_comparison(payload)
            if 'error' in result:
                return jsonify(result), 400
            buffer = build_crate_comparison_pdf(payload, result, project_name=project_name)
            download_name = 'AquaCell_Crate_Comparison.pdf'
        elif calc_type == 'site_overlay':
            image_data_url = data.get('image', '')
            if not image_data_url:
                return jsonify({'error': 'No overlay image was received.'}), 400
            meta = data.get('meta', {}) or {}
            buffer = build_site_overlay_pdf(image_data_url, project_name=project_name, meta=meta)
            download_name = 'AquaCell_Site_Overlay.pdf'
        elif calc_type == 'pt_row':
            result = calc_pt_row(payload)
            buffer = build_pt_row_pdf(payload, result, project_name=project_name)
            download_name = 'AquaCell_PT-ROW_Sizing.pdf'
        elif calc_type == 'stage_storage':
            # Stage-Storage is client-side by design — payload IS the browser's
            # already-computed result object, not raw inputs to recompute from.
            buffer = build_stage_storage_pdf(payload, project_name=project_name)
            download_name = 'AquaCell_Stage_Storage.pdf'
        else:
            return jsonify({'error': f'Unknown calc_type: {calc_type}'}), 400

        # Append sanitized project name to the filename (kept in sync with the
        # client-side acPdfName() helper in design_tools.html). Blank names are
        # a no-op, preserving the original base filename.
        if project_name:
            import re as _re
            _clean = _re.sub(r'[^A-Za-z0-9\-_ ]', '_', str(project_name)).strip()
            _clean = _re.sub(r'\s+', '_', _clean)
            _clean = _re.sub(r'_+', '_', _clean).strip('_')
            if _clean:
                if download_name.lower().endswith('.pdf'):
                    download_name = download_name[:-4] + '_' + _clean + '.pdf'
                else:
                    download_name = download_name + '_' + _clean

        return send_file(
            buffer,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=download_name
        )
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
        site_address    = data.get('site_address', '')
        client          = data.get('client', '')
        estimator       = data.get('estimator', '')
        estimator_email = data.get('estimator_email', '')
        project_notes   = data.get('project_notes', '')
        freight_pct     = float(data.get('freight_pct', 10) or 10)
        markup_pct      = float(data.get('markup_pct', 0) or 0)

        tanks_in     = data.get('tanks', [])
        tank_results = [calc_tank(t) for t in tanks_in]
        cum          = cumulative_bom(tank_results)

        # Pricing — component-based, computed client-side (mirrors single-tank).
        # JS is the source of truth for UNIT_PRICES; backend displays the sent values.
        tank_storage_total = cum['tank_storage']
        floor_cost     = float(data.get('floor_cost', 0) or 0)
        subtotal       = float(data.get('subtotal', 0) or 0)          # selling price (after markup)
        freight_cost   = float(data.get('freight_cost', 0) or 0)
        total_w_freight = float(data.get('total_with_freight', 0) or (subtotal + freight_cost))
        cost_per_ft3   = float(data.get('cost_per_ft3', 0) or 0)      # selling ÷ tank ft³ (display only)

        def money(v):
            return f'${v:,.2f}'

        contingency_override = data.get('contingency_override', None)
        if contingency_override is not None:
            contingency_all = max(0, int(contingency_override))
        else:
            contingency_all = max(0,
                math.ceil(cum['base_units'] / _MT_PALLETS['base']) * _MT_PALLETS['base'] - cum['base_units'])

        generated_str = datetime.datetime.now().strftime('%m/%d/%Y')
        logo_path = os.path.join(app.static_folder, 'aquacell-logo.png')

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

        cur_y = [H - 28]  # mutable for nested helpers

        def new_page_header(title):
            c.showPage()
            cur_y[0] = H - 28
            q_rect(LQ, cur_y[0] - 30, QW, 30, QNY)
            q_text(W / 2, cur_y[0] - 20, title, 'Helvetica-Bold', 10, WHITE, 'center')
            q_text(LQ + 4, cur_y[0] - 20, project_name or '—', 'Helvetica', 8, colors.HexColor('#93c5fd'))
            q_text(LQ + QW - 4, cur_y[0] - 20, generated_str, 'Helvetica', 8, colors.HexColor('#94a3b8'), 'right')
            cur_y[0] -= 38

        # ── PAGE 1: HEADER ───────────────────────────────────────────
        y = H - 28

        if logo_path and os.path.exists(logo_path):
            try:
                img = ImageReader(logo_path)
                c.drawImage(img, LQ, y - 44, width=130, height=44,
                            preserveAspectRatio=True, mask='auto')
            except Exception:
                pass
        q_text(LQ + 4, y - 52, 'An Orbia business.', 'Helvetica-Oblique', 7, GRAY)
        q_text(W / 2, y - 14, 'MULTI-TANK MATERIALS QUOTE', 'Helvetica-Bold', 18, QNY, 'center')
        q_text(W / 2, y - 28, f'{len(tank_results)} Tank(s)  |  Combined AquaCell System',
               'Helvetica', 9, GRAY, 'center')
        # Project number box — border only, no fill, so it does not mask the title
        c.setStrokeColor(QMGY)
        c.setLineWidth(0.8)
        c.rect(W - RQ - 130, y - 52, 130, 38, fill=0, stroke=1)
        q_text(W - RQ - 65, y - 28, project_num if project_num else '—', 'Helvetica-Bold', 16, QNY, 'center')
        q_text(W - RQ - 65, y - 44, 'PROJECT #', 'Helvetica', 7, GRAY, 'center')
        y -= 62
        q_rule(y)
        y -= 4

        # Project info grid
        q_text(LQ,        y - 8,  'CLIENT:',       'Helvetica-Bold', 8, QNY)
        q_text(LQ + 50,   y - 8,  client or '—',   'Helvetica', 8, BLACK)
        q_text(W / 2,     y - 8,  'DATE',           'Helvetica-Bold', 8, QNY)
        q_text(W/2 + 36,  y - 8,  generated_str,    'Helvetica', 8, BLACK)
        q_text(LQ,        y - 20, 'PROJECT NAME:',  'Helvetica-Bold', 8, QNY)
        q_text(LQ + 80,   y - 20, project_name or '—', 'Helvetica', 8, BLACK)
        q_text(W / 2,     y - 20, 'PREPARED BY:',   'Helvetica-Bold', 8, QNY)
        q_text(W/2 + 92,  y - 20, estimator or '—', 'Helvetica', 8, BLACK)
        q_text(LQ,        y - 32, 'LOCATION:',      'Helvetica-Bold', 8, QNY)
        q_text(LQ + 60,   y - 32, location or '—',  'Helvetica', 8, BLACK)
        q_text(W / 2,     y - 32, 'EMAIL:',         'Helvetica-Bold', 8, QNY)
        q_text(W/2 + 52,  y - 32, estimator_email or '—', 'Helvetica', 8, BLACK)
        q_text(LQ,        y - 44, 'SITE ADDRESS:',  'Helvetica-Bold', 8, QNY)
        q_text(LQ + 78,   y - 44, site_address or '—', 'Helvetica', 8, BLACK)
        y -= 56
        q_rule(y)
        y -= 8

        if project_notes.strip():
            q_rect(LQ, y - 14, QW, 14, QNY)
            q_text(W / 2, y - 10, project_notes[:120], 'Helvetica-Bold', 7, WHITE, 'center')
            y -= 18

        # Storage banner
        q_rect(LQ,        y - 28, QW / 2, 28, QNY)
        q_text(LQ + 6,    y - 10, 'TOTAL AQUACELL TANK STORAGE (ALL TANKS)', 'Helvetica-Bold', 7, WHITE)
        q_text(LQ + 6,    y - 22, f'{cum["tank_storage"]:,.0f} FT\u00b3', 'Helvetica-Bold', 12, QYLW)
        q_rect(LQ + QW/2, y - 28, QW / 2, 28, QTBL)
        q_text(LQ + QW/2 + 6, y - 10, 'COMBINED SYSTEM TOTAL STORAGE (TANK + STONE)', 'Helvetica-Bold', 7, WHITE)
        q_text(LQ + QW/2 + 6, y - 22, f'{cum["total_storage"]:,.0f} FT\u00b3', 'Helvetica-Bold', 12, WHITE)
        y -= 34

        # Per-tank summary table
        q_rect(LQ, y - 14, QW, 14, QNY)
        q_text(W / 2, y - 10, 'PER-TANK SUMMARY', 'Helvetica-Bold', 8.5, WHITE, 'center')
        y -= 14

        # Per-tank summary columns — W×L removed, space redistributed
        col_lbl = LQ + 4    # TANK label
        col_cfg = LQ + 75   # CONFIG
        col_ht  = LQ + 145  # HT(ft)
        col_sto = LQ + 205  # TANK FT³
        col_bu  = LQ + 300  # BASE
        col_sp  = LQ + 360  # SIDE
        col_bp  = LQ + 415  # BOT
        col_cov = LQ + QW - 4  # COVER (right-aligned)

        hdr_cols = [
            ('TANK',          col_lbl),
            ('CONFIG',        col_cfg),
            ('HT (ft)',       col_ht),
            ('TANK FT\u00b3', col_sto),
            ('BASE',          col_bu),
            ('SIDE',          col_sp),
            ('BOT',           col_bp),
            ('COVER DEPTH',   col_cov),
        ]
        q_rect(LQ, y - 13, QW, 13, colors.HexColor('#1a5276'))
        for lbl, x in hdr_cols:
            if lbl == 'COVER DEPTH':
                q_text(x, y - 9, lbl, 'Helvetica-Bold', 6.5, WHITE, 'right')
            else:
                q_text(x, y - 9, lbl, 'Helvetica-Bold', 6.5, WHITE)
        y -= 13

        for i, r in enumerate(tank_results):
            shade = QLGY if i % 2 == 0 else WHITE
            cov_str = f"{r['cover_depth']} ft  {'OK' if r['cover_ok'] else 'FAIL'}"
            q_rect(LQ, y - 13, QW, 13, shade)
            q_text(col_lbl, y - 9, r['tank_label'][:16],            'Helvetica-Bold', 7, QNY)
            q_text(col_cfg, y - 9, f"{r['config']}-{r['layers']}", 'Helvetica', 7, BLACK)
            q_text(col_ht,  y - 9, f"{r['tank_height']}",          'Helvetica', 7, BLACK)
            q_text(col_sto, y - 9, f"{r['tank_storage']:,.0f}",     'Helvetica-Bold', 7, QNY)
            q_text(col_bu,  y - 9, str(r['base_units']),            'Helvetica', 7, BLACK)
            q_text(col_sp,  y - 9, str(r['side_plates']),           'Helvetica', 7, BLACK)
            q_text(col_bp,  y - 9, str(r['bottom_plates']),         'Helvetica', 7, BLACK)
            q_text(col_cov, y - 9, cov_str, 'Helvetica', 7,
                   GREEN if r['cover_ok'] else RED, 'right')
            y -= 13

        # Totals row
        q_rect(LQ, y - 14, QW, 14, colors.HexColor('#1e3a8a'))
        q_text(col_lbl, y - 10, f"TOTAL ({len(tank_results)} TANKS)", 'Helvetica-Bold', 7, WHITE)
        q_text(col_sto, y - 10, f"{cum['tank_storage']:,.0f}", 'Helvetica-Bold', 8, QYLW)
        q_text(col_bu,  y - 10, str(cum['base_units']),    'Helvetica-Bold', 7, WHITE)
        q_text(col_sp,  y - 10, str(cum['side_plates']),   'Helvetica-Bold', 7, WHITE)
        q_text(col_bp,  y - 10, str(cum['bottom_plates']), 'Helvetica-Bold', 7, WHITE)
        y -= 20

        # Cumulative BOM section
        q_rect(LQ, y - 14, QW, 14, QNY)
        q_text(W / 2, y - 10,
               'CUMULATIVE BILL OF MATERIALS \u2014 AQUACELL COMPONENTS',
               'Helvetica-Bold', 8.5, WHITE, 'center')
        y -= 14

        bom_col_ln  = LQ + 4;   bom_col_pc = LQ + 32
        bom_col_ds  = LQ + 110; bom_col_qt = LQ + QW - 140
        bom_col_un  = LQ + QW - 80; bom_col_wt = LQ + QW - 4

        q_rect(LQ, y - 13, QW, 13, colors.HexColor('#1a5276'))
        for lbl, x in [('LINE', bom_col_ln), ('PART CODE', bom_col_pc),
                        ('DESCRIPTION', bom_col_ds), ('QTY', bom_col_qt),
                        ('UNITS', bom_col_un), ('WEIGHT (lbs)', bom_col_wt)]:
            if lbl == 'WEIGHT (lbs)':
                q_text(x, y - 9, lbl, 'Helvetica-Bold', 6.5, WHITE, 'right')
            else:
                q_text(x, y - 9, lbl, 'Helvetica-Bold', 6.5, WHITE)
        y -= 13

        wt_map = {
            'base_units':('base',25.18), 'side_plates':('side',4.91),
            'bottom_plates':('bottom',7.86), 'pipe_connectors':('pipe',2.70),
            'top_adapters_12':('adapter12',11.02), 'top_adapters_16':('adapter16',22.00),
            'contingency':('base',25.18),
        }
        bom_rows_q = [
            ('1', '3091506',    'AQUACELL BASE UNIT',                              cum['base_units'],      'EACH', 'base_units'),
            ('2', '2476600003', 'AQUACELL SIDE PLATE',                             cum['side_plates'],     'EACH', 'side_plates'),
            ('3', '2476600001', 'AQUACELL BOTTOM PLATE',                           cum['bottom_plates'],   'EACH', 'bottom_plates'),
            ('4', '2476631200', 'AQUACELL 8\u201312\u2033 PIPE CONNECTOR',         cum['pipe_connectors'], 'EACH', 'pipe_connectors'),
            ('5', '3085857',    'AQUACELL TOP CONNECTOR (12\u2033)',               cum['top_adapters_12'], 'EACH', 'top_adapters_12'),
            ('5', '2476842000', 'AQUACELL TOP CONNECTOR (16\u2033)',               cum['top_adapters_16'], 'EACH', 'top_adapters_16'),
            ('6', '3091506',    '**CONTINGENCY BASE UNITS (FULL PALLET ROUNDING)', contingency_all,        'EACH', 'contingency'),
        ]
        total_weight_all = 0
        for i, (ln, pc, ds, qt, un, wkey) in enumerate(bom_rows_q):
            shade = QLGY if i % 2 == 0 else WHITE
            q_rect(LQ, y - 13, QW, 13, shade)
            _, wt_each = wt_map.get(wkey, ('', 0))
            row_wt = round(qt * wt_each, 1) if qt else 0
            total_weight_all += row_wt
            q_text(bom_col_ln, y - 9, ln,  'Helvetica', 7, BLACK)
            q_text(bom_col_pc, y - 9, pc,  'Helvetica', 7, BLACK)
            q_text(bom_col_ds, y - 9, ds,  'Helvetica', 7, BLACK)
            q_text(bom_col_qt + 30, y - 9, str(qt) if qt else '0', 'Helvetica-Bold', 7.5, QNY, 'right')
            q_text(bom_col_un, y - 9, un,  'Helvetica', 7, BLACK)
            q_text(bom_col_wt, y - 9, f'{row_wt:,.1f}', 'Helvetica', 7, GRAY, 'right')
            y -= 13

        q_rect(LQ, y - 13, QW, 13, colors.HexColor('#1e3a8a'))
        q_text(bom_col_ds, y - 9, 'COMBINED SYSTEM WEIGHT', 'Helvetica-Bold', 7.5, WHITE)
        q_text(bom_col_wt, y - 9, f'{total_weight_all:,.1f} lbs', 'Helvetica-Bold', 8, QYLW, 'right')
        y -= 18

        # ════════════════════════════════════════
        #  PROVIDED BY OTHERS SECTION
        # ════════════════════════════════════════
        QRED_MT2 = colors.HexColor('#c0392b')
        q_rect(LQ, y - 13, QW, 13, QRED_MT2)
        q_text(W / 2, y - 9,
               'RECOMMENDED BY WAVIN — PROVIDED BY OTHERS  (ESTIMATES FOR REFERENCE ONLY — SUBJECT TO VERIFICATION)',
               'Helvetica-Bold', 7, WHITE, 'center')
        y -= 13

        # Others table header — LINE, DESCRIPTION, QUANTITY, UNITS
        oth_col_ln  = LQ + 4
        oth_col_ds  = LQ + 28
        oth_col_qt  = LQ + QW - 80
        oth_col_un  = LQ + QW - 4

        q_rect(LQ, y - 13, QW, 13, colors.HexColor('#922b21'))
        q_text(oth_col_ln, y - 9, 'LINE',        'Helvetica-Bold', 6.5, WHITE)
        q_text(oth_col_ds, y - 9, 'DESCRIPTION', 'Helvetica-Bold', 6.5, WHITE)
        q_text(oth_col_qt, y - 9, 'QUANTITY',    'Helvetica-Bold', 6.5, WHITE, 'right')
        q_text(oth_col_un, y - 9, 'UNITS',       'Helvetica-Bold', 6.5, WHITE, 'right')
        y -= 13

        # Cumulative geo conversion ft² → yd²
        cum_geoTank_yd2_adj = int(cum.get('geoTank_yd2_adj', round(cum['geoTank'] / 9, 0)))
        cum_geoStone_yd2 = int(round(cum['geoStone'] / 9, 0))
        cum_stone_yd3    = int(cum['stone_yd3'])
        cum_adapters     = cum['top_adapters_12'] + cum['top_adapters_16']
        geoWaste_pct     = tank_results[0]['geoWaste'] if tank_results else 20

        # Geogrid combined
        cum_geogrid_top  = int(cum.get('geogrid_top_yd2', 0))
        cum_geogrid_bot  = int(cum.get('geogrid_bottom_yd2', 0))
        cum_geogrid_total = cum_geogrid_top + cum_geogrid_bot
        geogrid_desc = (
            f'BIAXIAL GEOGRID (INTEGRALLY FORMED POLYPROPYLENE) + {geoWaste_pct}% WASTE'
            + (f'  [TOP: {cum_geogrid_top} SY + BOTTOM: {cum_geogrid_bot} SY]'
               if cum_geogrid_top > 0 and cum_geogrid_bot > 0 else '')
        )

        # Liner label
        cum_liner_tank  = int(cum.get('liner_tank_yd2', 0))
        cum_liner_stone = int(cum.get('liner_stone_yd2', 0))
        cum_liner_total = int(cum.get('liner_total_yd2', 0))
        any_liner_tank  = any(r.get('liner_on_tank',  False) for r in tank_results)
        any_liner_stone = any(r.get('liner_on_stone', False) for r in tank_results)
        if any_liner_tank and any_liner_stone:
            liner_desc = (f'WATERTIGHT GEOMEMBRANE LINER (MIN. 30 MIL) \u2014 TANK + STONE ENVELOPE'
                          f'  [{cum_liner_tank} SY TANK + {cum_liner_stone} SY STONE]')
        elif any_liner_tank:
            liner_desc = 'WATERTIGHT GEOMEMBRANE LINER (MIN. 30 MIL) \u2014 TANK ENVELOPE  (AQ-100-03.2)'
        elif any_liner_stone:
            liner_desc = 'WATERTIGHT GEOMEMBRANE LINER (MIN. 30 MIL) \u2014 STONE BACKFILL ENVELOPE  (AQ-100-03.4)'
        else:
            liner_desc = 'WATERTIGHT GEOMEMBRANE LINER (MIN. 30 MIL) \u2014 NOT SPECIFIED'

        # Liner protection non-woven (AQ-100-03.4 Note C) — one matching
        # non-woven layer per selected liner envelope. cum_liner_total already
        # equals cum_liner_tank + cum_liner_stone (waste baked in), so it is the
        # protection quantity. Mirrors the single-tank quote and the dashboard JS.
        if any_liner_tank and any_liner_stone:
            liner_prot_desc = (f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste_pct}% WASTE '
                               f'(LINER PROTECTION \u2014 TANK + STONE FACES, AQ-100-03.4)')
        elif any_liner_tank:
            liner_prot_desc = (f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste_pct}% WASTE '
                               f'(LINER PROTECTION \u2014 TANK ENVELOPE FACE, AQ-100-03.4)')
        elif any_liner_stone:
            liner_prot_desc = (f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste_pct}% WASTE '
                               f'(LINER PROTECTION \u2014 STONE ENVELOPE FACE, AQ-100-03.4)')
        else:
            liner_prot_desc = ''

        others_rows_mt = [
            ('A', f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste_pct}% WASTE (TANK ONLY)',
             cum_geoTank_yd2_adj, 'SQ YD'),
            ('B', f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste_pct}% WASTE (BACKFILL ONLY)',
             cum_geoStone_yd2, 'SQ YD'),
            ('C', f'WOVEN MONOFILAMENT GEOTEXTILE \u2014 PT-ROW\u2122 PRE-TREATMENT + {geoWaste_pct}% WASTE',
             int(cum.get('ptrow_woven_yd2', 0)), 'SQ YD'),
            ('D', geogrid_desc, cum_geogrid_total, 'SQ YD'),
            ('E', 'CASTINGS FOR VENTING / INSPECTION PORTS / INLETS', int(cum_adapters), 'EACH'),
            ('F', 'LARGE DIAMETER PIPE CONNECTION (18\u201336\u2033) \u2014 GEOTEXTILE BOOT / ABUTMENT',
             int(cum.get('large_pipe_qty', 0)), 'EACH'),
            ('G', 'STONE BACKFILL OR SELECT BACKFILL ESTIMATED FOR UG SYSTEM + 10%', cum_stone_yd3, 'CU YD'),
            ('H', liner_desc, cum_liner_total, 'SQ YD'),
        ]
        # Line I — liner protection non-woven, only when a liner is specified.
        if cum_liner_total > 0 and liner_prot_desc:
            others_rows_mt.append(('I', liner_prot_desc, cum_liner_total, 'SQ YD'))

        max_ds_chars = 90
        for i, (ln, ds, qt, un) in enumerate(others_rows_mt):
            shade = QLGY if i % 2 == 0 else WHITE
            rh = 20 if len(ds) > max_ds_chars else 13
            q_rect(LQ, y - rh, QW, rh, shade)
            q_text(oth_col_ln, y - (rh // 2) - 2, ln, 'Helvetica-Bold', 7, colors.HexColor('#922b21'))
            if len(ds) <= max_ds_chars:
                q_text(oth_col_ds, y - (rh // 2) - 2, ds, 'Helvetica', 7, BLACK)
            else:
                split = ds.rfind(' ', 0, max_ds_chars)
                if split == -1:
                    split = max_ds_chars
                q_text(oth_col_ds, y - 5,  ds[:split],      'Helvetica', 6.5, BLACK)
                q_text(oth_col_ds, y - 13, ds[split+1:],    'Helvetica', 6.5, BLACK)
            q_text(oth_col_qt, y - (rh // 2) - 2, str(qt) if qt else '0',
                   'Helvetica-Bold', 7.5, colors.HexColor('#922b21'), 'right')
            q_text(oth_col_un, y - (rh // 2) - 2, un, 'Helvetica', 7, BLACK, 'right')
            y -= rh

        y -= 8

        # Pricing block
        if subtotal > 0:
            _basis = ''
            if cost_per_ft3 > 0:
                _basis = f'${cost_per_ft3:.4f}/ft\u00b3 \u00d7 {tank_storage_total:,.1f} ft\u00b3 tank storage'
            q_rect(LQ, y - 13, QW - 120, 13, QLGY)
            q_text(LQ + 4, y - 9, _basis, 'Helvetica-Oblique', 7, GRAY)
            q_rect(LQ + QW - 120, y - 13, 120, 13, QNY)
            q_text(LQ + QW - 4, y - 9, 'AQUACELL SUB-TOTAL', 'Helvetica-Bold', 7, WHITE, 'right')
            y -= 13
            q_rect(LQ + QW - 120, y - 13, 120, 13, QLGY)
            q_text(LQ + QW - 4, y - 9, money(subtotal), 'Helvetica-Bold', 8.5, QNY, 'right')
            y -= 18

        QGRN_MT = colors.HexColor('#1a7a3a')
        QRED_MT = colors.HexColor('#c0392b')
        y -= 6
        totals_rows = [
            ('AQUACELL SUB-TOTAL',                    money(subtotal),            QNY,  QLGY),
            ('ESTIMATED TAXES*',                      '$0.00  (TBD at purchase)', GRAY, WHITE),
            (f'ESTIMATED FREIGHT* ({freight_pct:.1f}%)', money(freight_cost),
             colors.HexColor('#784212'), colors.HexColor('#fef9e7')),
            ('ESTIMATED TOTAL',                       money(total_w_freight),     QGRN_MT, colors.HexColor('#eafaf1')),
        ]
        tot_lbl_w = 160; tot_val_w = 110
        tx_lbl = LQ + QW - tot_lbl_w - tot_val_w - 4
        tx_val = LQ + QW - tot_val_w
        for lbl, val, fg, bg in totals_rows:
            q_rect(tx_lbl - 4, y - 15, tot_lbl_w + tot_val_w + 8, 15, bg)
            q_text(tx_lbl + tot_lbl_w - 4, y - 5, lbl, 'Helvetica-Bold', 8, fg, 'right')
            q_text(tx_val + tot_val_w - 4,  y - 5, val, 'Helvetica-Bold', 9, fg, 'right')
            q_rule(y - 15, thick=0.3, clr=QMGY)
            y -= 15

        y -= 8
        for note in [
            '*ESTIMATED TAXES & FREIGHT TO BE DETERMINED AT TIME OF PURCHASE',
            '*THIS QUOTE IS VALID FOR 30 DAYS FROM THE DATE OF ISSUANCE. SUBJECT TO CHANGE AFTER THIS DATE',
        ]:
            q_text(W / 2, y, note, 'Helvetica-Bold', 7, QRED_MT, 'center')
            y -= 11

        y -= 6
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

        # ── Quote page only — save and return ───────────────────────
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


# ══════════════════════════════════════════════════════════════════
#  ROUTE: /multi/download_price_csv  —  line-item pricing CSV for CS
# ══════════════════════════════════════════════════════════════════
@app.route('/multi/download_price_csv', methods=['POST'])
def multi_download_price_csv():
    try:
        import csv, io as _io
        data = request.get_json(force=True)

        project_name    = data.get('project_name', '')
        project_num     = data.get('project_num', '')
        client          = data.get('client', '')
        location        = data.get('location', '')
        site_address    = data.get('site_address', '')
        estimator       = data.get('estimator', '')
        estimator_email = data.get('estimator_email', '')
        generated_str   = datetime.datetime.now().strftime('%m/%d/%Y')

        tanks_in     = data.get('tanks', [])
        tank_results = [calc_tank(t) for t in tanks_in]
        cum          = cumulative_bom(tank_results)

        unit_prices  = data.get('unit_prices', {})
        p_base       = float(unit_prices.get('base',      42.47))
        p_side       = float(unit_prices.get('side',      21.31))
        p_bottom     = float(unit_prices.get('bottom',    18.71))
        p_pipe       = float(unit_prices.get('pipe',      28.53))
        p_adapter12  = float(unit_prices.get('adapter12', 96.54))
        p_adapter16  = float(unit_prices.get('adapter16', 170.60))

        markup_pct   = float(data.get('markup_pct', 0) or 0)
        markup_mult  = 1 / (1 - markup_pct / 100) if markup_pct < 100 else 1

        freight_pct  = float(data.get('freight_pct', 0) or 0)

        contingency_override = data.get('contingency_override', None)
        if contingency_override is not None:
            contingency_all = max(0, int(contingency_override))
        else:
            contingency_all = max(0,
                math.ceil(cum['base_units'] / _MT_PALLETS['base']) * _MT_PALLETS['base'] - cum['base_units'])

        def sell_price(floor_unit):
            return floor_unit * markup_mult

        rows = [
            ('1', '3091506',    'AquaCell Base Unit',                   cum['base_units'],      'EACH', p_base),
            ('2', '2476600003', 'AquaCell Side Plate',                  cum['side_plates'],     'EACH', p_side),
            ('3', '2476600001', 'AquaCell Bottom Plate',                cum['bottom_plates'],   'EACH', p_bottom),
            ('4', '2476631200', 'AquaCell 8-12" Pipe Connector',        cum['pipe_connectors'], 'EACH', p_pipe),
            ('5', '3085857',    'AquaCell Top Connector (12")',         cum['top_adapters_12'], 'EACH', p_adapter12),
            ('5', '2476842000', 'AquaCell Top Connector (16")',         cum['top_adapters_16'], 'EACH', p_adapter16),
            ('6', '3091506',    'AquaCell Base Unit - Contingency',     contingency_all,        'EACH', p_base),
        ]

        out = _io.StringIO()
        w   = csv.writer(out)

        # Project header block
        w.writerow(['AquaCell Order Pricing Export'])
        w.writerow(['Generated', generated_str])
        w.writerow(['Project Name', project_name])
        w.writerow(['Project #',    project_num])
        w.writerow(['Client',       client])
        w.writerow(['Location',     location])
        w.writerow(['Site Address', site_address])
        w.writerow(['Estimator',    estimator])
        w.writerow(['Email',        estimator_email])
        w.writerow([])

        # Column headers
        w.writerow(['Line', 'Part #', 'Description', 'Qty', 'Unit',
                    'Floor Unit Price', 'Sell Unit Price', 'Extended Floor', 'Extended Sell'])

        for ln, part, desc, qty, unit, unit_floor in rows:
            if qty == 0:
                continue
            unit_sell    = sell_price(unit_floor)
            ext_floor    = qty * unit_floor
            ext_sell     = qty * unit_sell
            w.writerow([ln, part, desc, qty, unit,
                        f'${unit_floor:,.2f}', f'${unit_sell:,.2f}',
                        f'${ext_floor:,.2f}',  f'${ext_sell:,.2f}'])

        # Totals
        total_floor = sum(r[5] * r[3] for r in rows if r[3] > 0)
        total_sell  = sum(sell_price(r[5]) * r[3] for r in rows if r[3] > 0)
        freight_est = total_sell * freight_pct / 100
        w.writerow([])
        w.writerow(['', '', '', '', 'TOTAL FLOOR COST', '', '', f'${total_floor:,.2f}', ''])
        w.writerow(['', '', '', '', 'TOTAL SELL PRICE', '', '', '', f'${total_sell:,.2f}'])
        w.writerow(['', '', '', '', f'ESTIMATED FREIGHT ({freight_pct:.1f}%)', '', '', '', f'${freight_est:,.2f}'])

        out.seek(0)
        buf = io.BytesIO(out.getvalue().encode('utf-8'))
        safe_name = (project_name or 'Project').strip().replace(' ', '_')
        safe_num  = (project_num or '').strip().replace(' ', '_')
        name_part = '_'.join(filter(None, [safe_num, safe_name]))
        return send_file(buf, as_attachment=True,
                         download_name=f'AquaCell_PriceExport_{name_part}_{datetime.datetime.now().strftime("%m%d%Y")}.csv',
                         mimetype='text/csv')

    except Exception as exc:
        import traceback
        return jsonify({'error': str(exc), 'trace': traceback.format_exc()}), 500


# ══════════════════════════════════════════════════════════════════
#  ROUTE: /multi/download_tank_summary  —  per-tank detail pages only
#  Separated from the quote so pricing is never accidentally shared.
# ══════════════════════════════════════════════════════════════════
@app.route('/multi/download_tank_summary', methods=['POST'])
def multi_download_tank_summary():
    try:
        data = request.get_json(force=True)

        project_name    = data.get('project_name', 'Project')
        project_num     = data.get('project_num', '')
        generated_str   = datetime.datetime.now().strftime('%m/%d/%Y')

        tanks_in     = data.get('tanks', [])
        tank_results = [calc_tank(t) for t in tanks_in]

        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=letter)
        W, H = letter

        QNY  = colors.HexColor('#003366')
        QLGY = colors.HexColor('#f2f2f2')
        QMGY = colors.HexColor('#d0d0d0')

        LQ = 28
        RQ = 28
        QW = W - LQ - RQ

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

        cur_y = [H - 28]
        first_page = [True]

        def new_page_header(title):
            if first_page[0]:
                first_page[0] = False
            else:
                c.showPage()
            cur_y[0] = H - 28
            q_rect(LQ, cur_y[0] - 30, QW, 30, QNY)
            q_text(W / 2, cur_y[0] - 20, title, 'Helvetica-Bold', 10, WHITE, 'center')
            q_text(LQ + 4, cur_y[0] - 20, project_name or '—', 'Helvetica', 8, colors.HexColor('#93c5fd'))
            q_text(LQ + QW - 4, cur_y[0] - 20, generated_str, 'Helvetica', 8, colors.HexColor('#94a3b8'), 'right')
            cur_y[0] -= 38

        # Per-tank detail pages
        for ti, r in enumerate(tank_results):
            _pg_title = f"Tank Detail: {r['tank_label']}  ({r['config']}-{r['layers']})"
            new_page_header(_pg_title)
            y = cur_y[0]

            def _ensure_space(px):
                # Start a continuation page if the next block would run off the sheet.
                nonlocal y
                if y - px < 56:
                    new_page_header(_pg_title + '  (cont.)')
                    y = cur_y[0]

            def detail_row_mt(label, value, shade=False, status=None):
                # status: None | 'PASS' | 'FAIL' -> colors the status word green/red.
                nonlocal y
                _ensure_space(13)
                rh = 13
                if shade:
                    c.setFillColor(LGRAY)
                    c.rect(LQ, y - rh + 3, QW, rh, fill=1, stroke=0)
                c.setFillColor(GRAY)
                c.setFont('Helvetica', 7.5)
                c.drawString(LQ + 5, y - 3, label)
                if status in ('PASS', 'FAIL'):
                    scol = GREEN if status == 'PASS' else RED
                    c.setFont('Helvetica-Bold', 7.5)
                    sw = c.stringWidth(status, 'Helvetica-Bold', 7.5)
                    c.setFillColor(scol)
                    c.drawRightString(LQ + QW - 5, y - 3, status)
                    if value:
                        c.setFillColor(BLACK)
                        c.drawRightString(LQ + QW - 5 - sw - 4, y - 3, str(value))
                else:
                    c.setFillColor(BLACK)
                    c.setFont('Helvetica-Bold', 7.5)
                    c.drawRightString(LQ + QW - 5, y - 3, str(value))
                c.setStrokeColor(MGRAY)
                c.setLineWidth(0.25)
                c.line(LQ, y - rh + 3, LQ + QW, y - rh + 3)
                y -= rh

            def section_hdr_mt(title):
                nonlocal y
                _ensure_space(15 + 13)   # header + at least one row
                bh = 15
                c.setFillColor(NAVY)
                c.rect(LQ, y - bh + 4, QW, bh, fill=1, stroke=0)
                c.setFillColor(WHITE)
                c.setFont('Helvetica-Bold', 8)
                c.drawString(LQ + 6, y - bh + 8, title)
                y -= bh + 2

            section_hdr_mt('\u25a0  STORAGE')
            detail_row_mt('AquaCell Tank Storage', f"{r['tank_storage']:,.1f} ft\u00b3", shade=False)
            detail_row_mt('Stone Backfill Storage', f"{r['stone_storage']:,.1f} ft\u00b3", shade=True)
            detail_row_mt('Total System Storage', f"{r['total_storage']:,.1f} ft\u00b3", shade=False)
            if r['min_storage'] > 0:
                detail_row_mt(f"Min Required Storage ({r['min_storage']:,.0f} ft\u00b3)", '',
                              shade=True, status=('PASS' if r['storage_ok'] else 'FAIL'))
            y -= 5

            section_hdr_mt('\u25a0  GEOMETRY')
            detail_row_mt('Snapped Tank (W \u00d7 L)', f"{r['tank_width']} ft \u00d7 {r['tank_length']} ft", shade=False)
            detail_row_mt('Crates per Layer', f"{r['crates_wide']} \u00d7 {r['crates_long']} = {r['crates_layer']}", shade=True)
            detail_row_mt('Number of Layers', str(r['layers']), shade=False)
            detail_row_mt('Tank Height', f"{r['tank_height']} ft", shade=True)
            detail_row_mt('Total System Depth', f"{r['total_system_depth']} ft", shade=False)
            detail_row_mt('Tank Perimeter', f"{r['used_perimeter']} ft", shade=True)
            y -= 5

            section_hdr_mt('\u25a0  COVER & LOAD')
            detail_row_mt('Surface Elevation', f"{r['surface_elev']} ft", shade=False)
            detail_row_mt('Tank Bottom Elevation', f"{r['tank_bottom_elev']} ft", shade=True)
            detail_row_mt('Tank Top Elevation', f"{r['tank_top_elev']} ft", shade=False)
            detail_row_mt(f"Cover Depth (min {r['min_cover']} ft)", f"{r['cover_depth']} ft \u2014",
                          shade=True, status=('PASS' if r['cover_ok'] else 'FAIL'))
            detail_row_mt('FoS \u2014 Dead Load', str(r['fos_dead'] or '\u2014'), shade=False)
            detail_row_mt('FoS \u2014 Live Load', str(r['fos_ll'] or '\u2014'), shade=True)
            y -= 5

            # ── Stone backfill (this tank) — per-layer net w/ inclusion ──
            section_hdr_mt('\u25a0  STONE BACKFILL  (provided by others \u2014 coordination estimates only)')
            _ensure_space(11)
            _sv_pct = int(round(r['stone_void'] * 100))
            c.setFillColor(GRAY)
            c.setFont('Helvetica-Oblique', 6.5)
            c.drawString(LQ + 5, y - 3,
                f"Storage by layer  (stone void ratio = {_sv_pct}%  |  net = gross \u00d7 void ratio  |  \u2713 = included in Total System Storage)")
            y -= 11

            _cs_g = LQ + QW - 250   # gross value right edge
            _cs_n = LQ + QW - 125   # net value right edge
            _cs_i = LQ + QW - 5     # included flag right edge

            # Header row
            _ensure_space(13)
            c.setFillColor(MGRAY)
            c.rect(LQ, y - 13 + 3, QW, 13, fill=1, stroke=0)
            c.setFillColor(NAVY)
            c.setFont('Helvetica-Bold', 6.8)
            c.drawString(LQ + 5, y - 3, 'Layer')
            c.drawRightString(_cs_g, y - 3, 'Gross (ft\u00b3)')
            c.drawRightString(_cs_n, y - 3, 'Net (ft\u00b3)')
            c.drawRightString(_cs_i, y - 3, 'In Net Total')
            y -= 13

            def stone_row_mt(label, gross, net, included, shade):
                nonlocal y
                _ensure_space(13)
                rh = 13
                if shade:
                    c.setFillColor(LGRAY)
                    c.rect(LQ, y - rh + 3, QW, rh, fill=1, stroke=0)
                c.setFillColor(GRAY)
                c.setFont('Helvetica', 7.5)
                c.drawString(LQ + 5, y - 3, label)
                c.setFillColor(GRAY)
                c.drawRightString(_cs_g, y - 3, f'{gross:,.1f}')
                c.setFillColor(BLACK)
                c.setFont('Helvetica-Bold', 7.5)
                c.drawRightString(_cs_n, y - 3, f'{net:,.1f}')
                c.setFont('Helvetica-Bold', 7)
                c.setFillColor(GREEN if included else RED)
                c.drawRightString(_cs_i, y - 3, '\u2713  INCLUDED' if included else '\u2014  EXCLUDED')
                c.setStrokeColor(MGRAY)
                c.setLineWidth(0.25)
                c.line(LQ, y - rh + 3, LQ + QW, y - rh + 3)
                y -= rh

            stone_row_mt('Cover / Top',       r['stone_top_gross'],   r['stone_top_net'],   r['stone_top_included'],   shade=False)
            stone_row_mt('Perimeter / Sides', r['stone_perim_gross'], r['stone_perim_net'], r['stone_perim_included'], shade=True)
            stone_row_mt('Base / Bottom',     r['stone_base_gross'],  r['stone_base_net'],  r['stone_base_included'],  shade=False)

            # Total (included layers) row
            _ensure_space(14)
            _gross_sum = r['stone_top_gross'] + r['stone_perim_gross'] + r['stone_base_gross']
            c.setFillColor(LTGRN)
            c.rect(LQ, y - 14 + 3, QW, 14, fill=1, stroke=0)
            c.setFillColor(GREEN)
            c.setFont('Helvetica-Bold', 7.5)
            c.drawString(LQ + 5, y - 4, 'TOTAL NET STORAGE (INCLUDED LAYERS)')
            c.drawRightString(_cs_g, y - 4, f'{_gross_sum:,.1f}')
            c.drawRightString(_cs_n, y - 4, f"{r['stone_layer_total_net']:,.1f} ft\u00b3")
            y -= 14 + 4

            section_hdr_mt('\u25a0  BILL OF MATERIALS \u2014 AQUACELL (THIS TANK)')
            detail_row_mt('Base Units (3091506)',          str(r['base_units']),       shade=False)
            detail_row_mt('Side Plates (2476600003)',      str(r['side_plates']),      shade=True)
            detail_row_mt('Bottom Plates (2476600001)',    str(r['bottom_plates']),    shade=False)
            detail_row_mt('8-12" Pipe Connectors (2476631200)', str(r['pipe_connectors']), shade=True)
            detail_row_mt('12" Top Adapters (3085857)',   str(r['top_adapters_12']),  shade=False)
            detail_row_mt('16" Top Adapters (2476842000)', str(r['top_adapters_16']), shade=True)
            y -= 5

            # ── Accessories / provided-by-others (this tank) — full one-sheet BOM ──
            section_hdr_mt('\u25a0  ACCESSORIES & PROVIDED BY OTHERS (THIS TANK)')
            _sh = [False]
            def _acc(label, value):
                detail_row_mt(label, value, shade=_sh[0])
                _sh[0] = not _sh[0]
            # Geotextile (always present)
            _acc('Non-Woven Geotextile \u2014 Tank Wrap',
                 f"{r['geoTank_yd2']:,.1f} yd\u00b2  ({r['geoTank']:,.1f} ft\u00b2)")
            _acc('Non-Woven Geotextile \u2014 Stone / Backfill',
                 f"{r['geoStone_yd2']:,.1f} yd\u00b2  ({r['geoStone']:,.1f} ft\u00b2)")
            # Stone backfill (always present)
            _acc('Stone Backfill (#57 or select)',
                 f"{r['stone_yd3']:,.1f} cu yd  ({r['stone_tons']:,.1f} tons)")
            # PT-ROW pre-treatment (if applicable)
            if r.get('ptrow_enabled') and r.get('ptrow_total_crates', 0) > 0:
                _acc('PT-ROW\u2122 Pre-Treatment Crates', f"{r['ptrow_total_crates']:,}")
                if r.get('ptrow_woven_yd2', 0) > 0:
                    _acc('PT-ROW\u2122 Woven Monofilament Geotextile', f"{r['ptrow_woven_yd2']:,.1f} yd\u00b2")
            # Biaxial geogrid (if applicable)
            _geogrid_total = (r.get('geogrid_top_yd2', 0) or 0) + (r.get('geogrid_bottom_yd2', 0) or 0)
            if _geogrid_total > 0:
                _detail = []
                if r.get('geogrid_top_yd2', 0) > 0:    _detail.append(f"top {r['geogrid_top_yd2']:,.1f}")
                if r.get('geogrid_bottom_yd2', 0) > 0: _detail.append(f"bot {r['geogrid_bottom_yd2']:,.1f}")
                _suffix = f"  ({', '.join(_detail)})" if _detail else ''
                _acc('Biaxial Geogrid', f"{_geogrid_total:,.1f} yd\u00b2{_suffix}")
            # PVC / geomembrane liner (if applicable)
            if (r.get('liner_total_yd2', 0) or 0) > 0:
                _lscope = []
                if r.get('liner_on_tank'):  _lscope.append('tank')
                if r.get('liner_on_stone'): _lscope.append('stone')
                _lsfx = f"  ({' + '.join(_lscope)})" if _lscope else ''
                _acc('PVC / Geomembrane Liner', f"{r['liner_total_yd2']:,.1f} yd\u00b2{_lsfx}")
            # Large diameter pipe connections (if applicable)
            if (r.get('large_pipe_qty', 0) or 0) > 0:
                _acc('Large Diameter Pipe Connections (18\u201336\u2033)', f"{r['large_pipe_qty']:,} ea")

            # ── Tank notes (this tank only) ──
            _tnotes = (r.get('tank_notes', '') or '').strip()
            if _tnotes:
                y -= 5
                section_hdr_mt('\u25a0  TANK NOTES')
                _ensure_space(13)
                c.setFillColor(BLACK)
                c.setFont('Helvetica', 8)
                _max_w = QW - 12
                for _para in _tnotes.split('\n'):
                    _para = _para.rstrip()
                    if not _para:
                        y -= 6
                        continue
                    _line = ''
                    for _word in _para.split():
                        _test = (_line + ' ' + _word).strip()
                        if c.stringWidth(_test, 'Helvetica', 8) <= _max_w:
                            _line = _test
                        else:
                            _ensure_space(12)
                            c.setFillColor(BLACK); c.setFont('Helvetica', 8)
                            c.drawString(LQ + 6, y - 3, _line)
                            y -= 12
                            _line = _word
                    if _line:
                        _ensure_space(12)
                        c.setFillColor(BLACK); c.setFont('Helvetica', 8)
                        c.drawString(LQ + 6, y - 3, _line)
                        y -= 12

        c.save()
        buffer.seek(0)

        safe_name = (project_name or 'Project').strip().replace(' ', '_')
        safe_num  = (project_num or '').strip().replace(' ', '_')
        name_part = '_'.join(filter(None, [safe_num, safe_name]))
        return send_file(
            buffer, as_attachment=True,
            download_name=f'AquaCell_MultiTank_Summary_{name_part}_{datetime.datetime.now().strftime("%m%d%Y")}.pdf',
            mimetype='application/pdf'
        )

    except Exception as exc:
        import traceback
        return jsonify({'error': str(exc), 'trace': traceback.format_exc()}), 500


# ══════════════════════════════════════════════════════════════════
#  ROUTE: /multi/download_stage_csvs  —  per-tank stage-storage CSVs (ZIP)
#  One CSV per calculated tank, bundled into a single ZIP. Uses each
#  tank's own config/depths. Not shown on the dashboard or in the PDF.
# ══════════════════════════════════════════════════════════════════
@app.route('/multi/download_stage_csvs', methods=['POST'])
def multi_download_stage_csvs():
    import csv
    import io as _io
    import zipfile
    import re as _re
    try:
        data = request.get_json(force=True)
        tanks_in = data.get('tanks', [])
        if not tanks_in:
            return jsonify({'error': 'No tanks provided'}), 400

        project_name = (data.get('project_name', '') or 'Project').strip()
        try:
            stage_increment_in = int(data.get('stage_increment_in', 12) or 12)
        except (TypeError, ValueError):
            stage_increment_in = 12
        if stage_increment_in <= 0:
            stage_increment_in = 12
        increment_ft = stage_increment_in / 12.0

        def _safe(s, fallback):
            s = _re.sub(r'[^A-Za-z0-9\-_]+', '_', (str(s) or '').strip()).strip('_')
            return s or fallback

        zip_buf = _io.BytesIO()
        used_names = set()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for idx, t in enumerate(tanks_in):
                calc = calc_tank(t)
                tank_height        = calc['tank_height']
                total_system_depth = calc['total_system_depth']
                tank_bottom_elev   = calc['tank_bottom_elev']
                base_stone         = calc['base_stone']
                cover_stone        = calc['cover_stone']
                tank_storage       = calc['tank_storage']
                stone_storage      = calc['stone_storage']
                config             = calc['config']
                layers             = calc['layers']
                label              = calc.get('tank_label', '') or f'Tank {idx + 1}'

                # Build the stage table (proportional fill across elevation)
                rows = []
                top_of_stone = tank_bottom_elev + tank_height + cover_stone
                current_elev = tank_bottom_elev - base_stone
                while current_elev <= top_of_stone + 0.01:
                    depth_tank  = max(0, min(tank_height, current_elev - tank_bottom_elev))
                    tank_vol    = (depth_tank / tank_height) * tank_storage if tank_height > 0 else 0
                    depth_stone = max(0, min(total_system_depth, current_elev - (tank_bottom_elev - base_stone)))
                    stone_vol   = (depth_stone / total_system_depth) * stone_storage if total_system_depth > 0 else 0
                    rows.append((round(current_elev, 4), round(tank_vol, 2),
                                 round(stone_vol, 2), round(tank_vol + stone_vol, 2)))
                    current_elev += increment_ft

                out = _io.StringIO()
                writer = csv.writer(out)
                writer.writerow(['# AquaCell Stage-Storage Table'])
                writer.writerow([f'# Project: {project_name}'])
                writer.writerow([f'# Tank: {label}'])
                writer.writerow([f'# Configuration: {config}-{layers}  |  Stage Increment: {stage_increment_in} in'])
                writer.writerow([f'# Tank Bottom Elev: {tank_bottom_elev} ft  |  Tank Height: {tank_height} ft'
                                 f'  |  Base Stone: {base_stone} ft  |  Cover Stone: {cover_stone} ft'])
                writer.writerow([f'# AquaCell Tank Storage: {tank_storage} ft3  |  Stone Storage: {stone_storage} ft3'])
                writer.writerow([f'# Generated: {datetime.datetime.now().strftime("%m/%d/%Y %H:%M")}'])
                writer.writerow([])
                writer.writerow(['Elevation (ft)', 'Tank Storage (ft3)', 'Stone Storage (ft3)', 'Total Storage (ft3)'])
                for row in rows:
                    writer.writerow(row)

                # Unique per-tank filename inside the zip
                base = f'{idx + 1:02d}_{_safe(label, "Tank")}_StageStorage'
                name = base + '.csv'
                dup = 2
                while name in used_names:
                    name = f'{base}_{dup}.csv'
                    dup += 1
                used_names.add(name)
                zf.writestr(name, out.getvalue())

        zip_buf.seek(0)
        proj = _safe(project_name, 'Project')
        return send_file(
            zip_buf,
            as_attachment=True,
            download_name=f'AquaCell_MultiTank_StageStorage_{proj}_{datetime.datetime.now().strftime("%m%d%Y")}.zip',
            mimetype='application/zip'
        )
    except Exception as exc:
        import traceback
        return jsonify({'error': str(exc), 'trace': traceback.format_exc()}), 500


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
    estimator         = request.form.get('estimator', '')
    estimator_email   = request.form.get('estimator_email', '')
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
        complex_gross_area = snapped_known * snapped_other

        # ── Void Space Entry ──────────────────────────────────────
        vs = parse_void_spaces(request.form, perimeter_stone_width, tank_height)
        complex_tank_area = max(0.0, complex_gross_area - vs['total_area'])
        crates_per_layer  = max(0, crates_known * crates_other - vs['total_crates_layer'])
        effective_perim   = complex_tank_perim + (vs['total_perim'] if vs['active'] else 0.0)

        num_crates        = crates_per_layer * layers
        gross_tank_vol    = complex_tank_area * tank_height
        tank_storage      = gross_tank_vol * void_ratio
        tank_perim_calc   = effective_perim
        if complex_excav_area <= 0:
            complex_excav_area = (snapped_known + 2*perimeter_stone_width) * (snapped_other + 2*perimeter_stone_width)
        if complex_excav_perim <= 0:
            complex_excav_perim = complex_tank_perim + 8*perimeter_stone_width
        total_excavation_vol  = complex_excav_area * total_system_depth
        stone_envelope_volume = total_excavation_vol - gross_tank_vol
        void_fill_vol      = vs['fill_vol'] if vs['active'] else 0.0
        stone_storage_env  = max(0.0, stone_envelope_volume - void_fill_vol)
        total_stone_storage   = stone_storage_env * stone_void
        total_storage         = tank_storage + total_stone_storage
        geoTank  = round((2*complex_tank_area + effective_perim*tank_height) * (1 + geoWaste/100.0), 1)
        geoStone = round((2*complex_excav_area + complex_excav_perim*total_system_depth) * (1 + geoWaste/100.0), 1)
        geoTotal = round(geoTank + geoStone, 1)
        used_perimeter = effective_perim
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
        complex_gross_area  = None
        crates_known = crates_wide   # alias for display
        crates_other = crates_long   # alias for display
        vs                  = dict(_VOID_INERT)
        void_fill_vol       = 0.0

    stone_envelope_with_overage = stone_envelope_volume * 1.10

    if shape_mode == 'complex':
        excav_area_for_layers = complex_excav_area
        tank_footprint        = complex_tank_area
    else:
        excav_area_for_layers = outer_width * outer_length
        tank_footprint        = tank_width * tank_length

    # Entire void footprint excluded from Perimeter stone layer (no credit)
    _void_area_layers = vs['total_area'] if vs['active'] else 0.0
    stone_top_gross      = round(excav_area_for_layers * cover_stone, 1)
    stone_top_net        = round(stone_top_gross * stone_void, 1)
    stone_perim_gross    = round((excav_area_for_layers - tank_footprint - _void_area_layers) * tank_height, 1)
    stone_perim_net      = round(stone_perim_gross * stone_void, 1)
    stone_base_gross     = round(excav_area_for_layers * base_stone, 1)
    stone_base_net       = round(stone_base_gross * stone_void, 1)
    void_fill_ft3 = round(void_fill_vol, 1) if vs['active'] else 0.0
    void_fill_yd3 = round(void_fill_ft3 / 27, 2)

    # ── Stone layer net-storage inclusion toggles ──────────────────
    # JS sends '1' (included) or '0' (excluded) for each layer.
    # Gross volumes are always preserved for purchase/coordination.
    stone_top_included   = request.form.get('stone_top_included',   '1') == '1'
    stone_perim_included = request.form.get('stone_perim_included', '1') == '1'
    stone_base_included  = request.form.get('stone_base_included',  '1') == '1'

    stone_top_net_pdf   = stone_top_net   if stone_top_included   else 0.0
    stone_perim_net_pdf = stone_perim_net if stone_perim_included else 0.0
    stone_base_net_pdf  = stone_base_net  if stone_base_included  else 0.0
    stone_layer_total_net = round(stone_top_net_pdf + stone_perim_net_pdf + stone_base_net_pdf, 1)

    # Adjust total_stone_storage and total_storage to match toggled selection
    total_stone_storage   = stone_layer_total_net
    total_storage         = round(tank_storage + total_stone_storage, 1)

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
    y = _draw_page1_header(c, logo_path, project_name, project_num, location, client, estimator, generated_str)

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
    y = _kv_row(c, y, 'Stone Backfill Storage  (included layers net storage)', f'{total_stone_storage:,.1f} ft³', shade=False)
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
                    f'{tank_width} ft × {tank_length} ft  (= {round(complex_gross_area,1)} ft²)',
                    shade=True)
        if vs['active']:
            y = _kv_row(c, y,
                        f"Void Spaces ({len(vs['voids'])} — snapped UP to whole crates)",
                        f"−{round(vs['total_area'],1)} ft²  |  +{round(vs['total_perim'],1)} ft perimeter  |  −{vs['total_crates_layer']} crates/layer",
                        val_color=AMBER, shade=False)
            y = _kv_row(c, y, 'Net Tank Area (after voids)',
                        f'{round(complex_tank_area,1)} ft²', shade=True)
            y = _kv_row(c, y, 'Void Fill (by others — no storage credit)',
                        f'{round(void_fill_vol,1):,.1f} ft³  ({round(void_fill_vol/27,2):,.2f} yd³)',
                        val_color=AMBER, shade=False)
            y = _kv_row(c, y, 'Excavation Envelope Area', f'{round(complex_excav_area,1)} ft²', shade=True)
            y = _kv_row(c, y, 'Excavation Envelope Perimeter', f'{round(complex_excav_perim,1)} ft', shade=False)
        else:
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

    y = _sub_label(c, y, f'STORAGE BY LAYER  (stone void ratio = {stone_void_pct}%  |  net = gross × void ratio  |  ✓ = included in Total System Storage)')

    # Column anchors for 4-column stone table
    C_STN_LAYER = LM + 5
    C_STN_GROSS_R = LM + CW - 215
    C_STN_NET_R   = LM + CW - 105
    C_STN_INC_R   = LM + CW - 5
    COL_W_NUM = 100
    COL_W_INC = 90

    y = _table_header(c, y, [
        ('Layer',             C_STN_LAYER,              160,       'left'),
        ('Gross (ft³)',       C_STN_GROSS_R - COL_W_NUM, COL_W_NUM, 'right'),
        ('Net (ft³)',         C_STN_NET_R   - COL_W_NUM, COL_W_NUM, 'right'),
        ('In Net Total',      C_STN_INC_R   - COL_W_INC, COL_W_INC, 'right'),
    ])

    def _stone_row(y, label, gross, net, included, shade):
        inc_text  = '✓  INCLUDED' if included else '—  EXCLUDED'
        inc_color = GREEN         if included else RED
        y = _table_row(c, y, [
            (label,           C_STN_LAYER,              160,       'left',  False, None),
            (f'{gross:,.1f}', C_STN_GROSS_R - COL_W_NUM, COL_W_NUM, 'right', False, GRAY),
            (f'{net:,.1f}',   C_STN_NET_R   - COL_W_NUM, COL_W_NUM, 'right', False, None),
            (inc_text,        C_STN_INC_R   - COL_W_INC, COL_W_INC, 'right', True,  inc_color),
        ], shade=shade)
        return y

    y = _stone_row(y, 'Cover / Top',     stone_top_gross,   stone_top_net,   stone_top_included,   shade=False)
    y = _stone_row(y, 'Perimeter / Sides', stone_perim_gross, stone_perim_net, stone_perim_included, shade=True)
    y = _stone_row(y, 'Base / Bottom',   stone_base_gross,  stone_base_net,  stone_base_included,  shade=False)
    y = _table_total_row(c, y, [
        ('TOTAL NET STORAGE (INCLUDED LAYERS)',    C_STN_LAYER,              160,       'left'),
        (f'{stone_top_gross + stone_perim_gross + stone_base_gross:,.1f}',
                                                   C_STN_GROSS_R - COL_W_NUM, COL_W_NUM, 'right'),
        (f'{stone_layer_total_net:,.1f} ft³',      C_STN_NET_R   - COL_W_NUM, COL_W_NUM, 'right'),
        ('',                                       C_STN_INC_R   - COL_W_INC, COL_W_INC, 'right'),
    ], bg=LTGRN, fg=GREEN)
    if vs['active']:
        y = _kv_row(c, y,
                    'VOID FILL REQUIRED — by others (no storage credit)',
                    f'{void_fill_ft3:,.1f} ft³  ({void_fill_yd3:,.2f} yd³)',
                    val_color=AMBER, shade=True)
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
                     download_name=f'AquaCell_Summary_{safe_name}_{datetime.datetime.now().strftime("%m%d%Y")}.pdf',
                     mimetype='application/pdf')




# ══════════════════════════════════════════════════════════════════
#  DOWNLOAD STAGE STORAGE CSV
# ══════════════════════════════════════════════════════════════════

@app.route('/download_stage_csv', methods=['POST'])
def download_stage_csv():
    import csv
    import io as _io

    project_name       = request.form.get('project_name', 'Project')
    config             = request.form.get('config', 'SC')
    layers             = int(request.form.get('layers', 3))
    surface_elev       = float(request.form.get('surface_elev', 0) or 0)
    tank_bottom_elev   = float(request.form.get('tank_bottom_elev', 0) or 0)
    known_width        = float(request.form.get('known_width', 0) or 0)
    known_length       = float(request.form.get('known_length', 0) or 0)
    perimeter_stone_width = float(request.form.get('perimeter_stone_width', 1.0) or 1.0)
    cover_stone        = float(request.form.get('cover_stone', 1.0) or 1.0)
    base_stone         = float(request.form.get('base_stone', 0.333) or 0.333)
    stone_void         = float(request.form.get('stone_void', 0.40) or 0.40)
    stage_increment_in = int(request.form.get('stage_increment_in', 12) or 12)
    shape_mode         = request.form.get('shape_mode', 'rectangle')

    MODULE_WID = 1.9685
    MODULE_LEN = 3.937

    if config == 'SC':
        layer_heights = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
        void_ratio    = 0.95486
    else:
        layer_heights = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
        void_ratio    = 0.92633

    tank_height        = layer_heights[layers-1] if layers <= len(layer_heights) else layer_heights[-1]
    total_system_depth = base_stone + tank_height + cover_stone

    if shape_mode == 'complex':
        complex_scaled_area = float(request.form.get('complex_scaled_area', 0) or 0)
        complex_known_dim   = float(request.form.get('complex_known_dim', 0) or 0)
        complex_excav_area  = float(request.form.get('complex_excav_area', 0) or 0)
        other_dim     = complex_scaled_area / complex_known_dim if complex_known_dim > 0 else 0
        crates_known  = math.floor(complex_known_dim / MODULE_WID) if complex_known_dim > 0 else 0
        crates_other  = math.floor(other_dim / MODULE_LEN) if other_dim > 0 else 0
        snapped_known = crates_known * MODULE_WID
        snapped_other = crates_other * MODULE_LEN
        # ── Void Space Entry — net the tank, exclude core from storage ──
        vs = parse_void_spaces(request.form, perimeter_stone_width, tank_height)
        _net_tank_area = max(0.0, snapped_known * snapped_other - vs['total_area'])
        gross_vol     = _net_tank_area * tank_height
        tank_storage  = gross_vol * void_ratio
        if complex_excav_area <= 0:
            complex_excav_area = (snapped_known + 2*perimeter_stone_width) * (snapped_other + 2*perimeter_stone_width)
        void_fill_vol = vs['fill_vol'] if vs['active'] else 0.0
        stone_envelope_volume = max(0.0, complex_excav_area * total_system_depth - gross_vol - void_fill_vol)
    else:
        crates_wide   = math.floor(known_width / MODULE_WID)
        crates_long   = math.floor(known_length / MODULE_LEN)
        tank_width    = crates_wide * MODULE_WID
        tank_length   = crates_long * MODULE_LEN
        gross_vol     = tank_width * tank_length * tank_height
        tank_storage  = gross_vol * void_ratio
        outer_width   = tank_width + 2*perimeter_stone_width
        outer_length  = tank_length + 2*perimeter_stone_width
        stone_envelope_volume = outer_width * outer_length * total_system_depth - gross_vol
        vs            = dict(_VOID_INERT)
        void_fill_vol = 0.0

    total_stone_storage = stone_envelope_volume * stone_void

    # ── Stone layer inclusion toggles ─────────────────────────────
    stone_top_included   = request.form.get('stone_top_included',   '1') == '1'
    stone_perim_included = request.form.get('stone_perim_included', '1') == '1'
    stone_base_included  = request.form.get('stone_base_included',  '1') == '1'

    # Compute full net volumes by layer so we can apply the toggle ratio
    if shape_mode == 'complex':
        _excav_area = complex_excav_area
        _tank_fp    = _net_tank_area
        _void_area  = vs['total_area'] if vs['active'] else 0.0
    else:
        _excav_area = outer_width * outer_length
        _tank_fp    = tank_width * tank_length
        _void_area  = 0.0

    _top_net   = round(_excav_area * cover_stone * stone_void, 4)
    _perim_net = round((_excav_area - _tank_fp - _void_area) * tank_height * stone_void, 4)
    _base_net  = round(_excav_area * base_stone * stone_void, 4)
    _full_net  = _top_net + _perim_net + _base_net

    _incl_net  = ((  _top_net if stone_top_included   else 0)
               + (_perim_net if stone_perim_included else 0)
               + ( _base_net if stone_base_included  else 0))

    stone_ratio = _incl_net / _full_net if _full_net > 0 else 0.0
    total_stone_storage_csv = total_stone_storage * stone_ratio

    # Build the toggle label for the CSV header
    incl_labels = []
    if stone_top_included:   incl_labels.append('Top')
    if stone_perim_included: incl_labels.append('Perimeter')
    if stone_base_included:  incl_labels.append('Base')
    incl_str = ', '.join(incl_labels) if incl_labels else 'None'

    # Build rows
    increment_ft  = stage_increment_in / 12.0
    top_of_stone  = tank_bottom_elev + tank_height + cover_stone
    current_elev  = tank_bottom_elev - base_stone
    rows = []
    while current_elev <= top_of_stone + 0.01:
        depth_tank  = max(0, min(tank_height, current_elev - tank_bottom_elev))
        tank_vol    = (depth_tank / tank_height) * tank_storage if tank_height > 0 else 0
        depth_stone = max(0, min(total_system_depth, current_elev - (tank_bottom_elev - base_stone)))
        stone_vol   = (depth_stone / total_system_depth) * total_stone_storage_csv if total_system_depth > 0 else 0
        rows.append((round(current_elev, 4), round(tank_vol, 2), round(stone_vol, 2), round(tank_vol + stone_vol, 2)))
        current_elev += increment_ft

    # Write CSV with metadata header
    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['# AquaCell Stage-Storage Table'])
    writer.writerow([f'# Project: {project_name}'])
    writer.writerow([f'# Configuration: {config}-{layers}  |  Stage Increment: {stage_increment_in} in'])
    writer.writerow([f'# Tank Bottom Elev: {tank_bottom_elev} ft  |  Surface Elev: {surface_elev} ft'])
    writer.writerow([f'# Stone Layers Included in Net Total: {incl_str}'])
    if vs['active']:
        writer.writerow([f"# Void Spaces: {len(vs['voids'])}  |  -{round(vs['total_area'],1)} ft2 tank area  |  Void Fill by others - NO storage credit"])
    writer.writerow([f'# Generated: {datetime.datetime.now().strftime("%m/%d/%Y %H:%M")}'])
    writer.writerow([])
    writer.writerow(['Elevation (ft)', 'Tank Storage (ft3)', 'Stone Storage (ft3)', 'Total Storage (ft3)'])
    for row in rows:
        writer.writerow(row)

    output.seek(0)
    safe_name = (project_name or 'Project').strip().replace(' ', '_')
    return send_file(
        _io.BytesIO(output.getvalue().encode('utf-8')),
        as_attachment=True,
        download_name=f'AquaCell_StageStorage_{safe_name}_{datetime.datetime.now().strftime("%m%d%Y")}.csv',
        mimetype='text/csv'
    )


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
      site_address     = request.form.get('site_address', '')
      client           = request.form.get('client', '')
      estimator        = request.form.get('estimator', '')
      estimator_email  = request.form.get('estimator_email', '')
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
      contingency_units    = int(request.form.get('contingency_units', 0) or 0)
      contingency_overridden = request.form.get('contingency_overridden', '0')
      project_notes    = request.form.get('project_notes', '')
      shape_mode       = request.form.get('shape_mode', 'rectangle')

      # ── Hardcoded floor unit prices — update here on price change ──
      FLOOR_PRICES = {
          'base':      42.47,
          'side':      21.31,
          'bottom':    18.71,
          'pipe':      28.53,
          'adapter12': 96.54,
          'adapter16': 170.60,
      }

      # Pricing from form
      floor_cost         = float(request.form.get('floorCost', 0) or 0)
      subtotal           = float(request.form.get('totalAquaCellCost', 0) or 0)   # selling price (after markup)
      freight_cost       = float(request.form.get('freightCost', 0) or 0)
      total_with_freight = float(request.form.get('totalWithFreight', 0) or subtotal)
      freight_pct        = float(request.form.get('freightPct', 10) or 10)
      markup_pct         = float(request.form.get('markupPct', 0) or 0)

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
          complex_gross_area = snapped_known * snapped_other

          # ── Void Space Entry ──────────────────────────────────
          vs = parse_void_spaces(request.form, perimeter_stone_width, tank_height)
          complex_tank_area = max(0.0, complex_gross_area - vs['total_area'])
          crates_per_layer  = max(0, crates_known * crates_other - vs['total_crates_layer'])
          effective_perim   = complex_tank_perim + (vs['total_perim'] if vs['active'] else 0.0)

          num_crates        = crates_per_layer * layers
          gross_tank_vol    = complex_tank_area * tank_height
          tank_storage      = gross_tank_vol * void_ratio
          tank_perim_calc   = effective_perim
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
          void_fill_vol      = vs['fill_vol'] if vs['active'] else 0.0
          stone_storage_env  = max(0.0, stone_envelope_volume - void_fill_vol)
          total_stone_storage   = stone_storage_env * stone_void
          total_storage         = tank_storage + total_stone_storage
          geoTank  = round((2*complex_tank_area + effective_perim*tank_height) * (1 + geoWaste/100.0), 1)
          geoStone = round((2*complex_excav_area + complex_excav_perim*total_system_depth) * (1 + geoWaste/100.0), 1)
          used_perimeter = effective_perim
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
          vs                  = dict(_VOID_INERT)
          void_fill_vol       = 0.0

      geoTank_yd2  = round(geoTank  / 9, 0)
      geoStone_yd2 = round(geoStone / 9, 0)
      stone_yd3    = round(stone_envelope_volume * 1.10 / 27, 0)
      void_fill_yd3 = round((void_fill_vol if vs['active'] else 0.0) / 27, 1)

      # ── Geogrid quantities from UI inputs ──────────────────────────
      geogrid_top_yd2    = int(request.form.get('geogrid_top_yd2',    0) or 0)
      geogrid_bottom_yd2 = int(request.form.get('geogrid_bottom_yd2', 0) or 0)
      geogrid_total_yd2  = geogrid_top_yd2 + geogrid_bottom_yd2

      # ── Non-woven deduction: geogrid bottom substitutes for fabric on tank floor ──
      # Line A (tank-only non-woven) is reduced by the bottom geogrid area.
      # Line B (backfill envelope) is never affected.
      geoTank_yd2_adj = max(0, int(geoTank_yd2) - geogrid_bottom_yd2)
      nw_tank_label = (
          f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste}% WASTE (TANK ONLY — EXCL. BOTTOM: GEOGRID SUB.)'
          if geogrid_bottom_yd2 > 0 else
          f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste}% WASTE (TANK ONLY)'
      )

      # ── PVC / Geomembrane liner (line H) ───────────────────────────
      liner_on_tank   = request.form.get('liner_on_tank',   '0') == '1'
      liner_on_stone  = request.form.get('liner_on_stone',  '0') == '1'
      liner_tank_yd2  = int(request.form.get('liner_tank_yd2',  0) or 0)
      liner_stone_yd2 = int(request.form.get('liner_stone_yd2', 0) or 0)
      liner_total_yd2 = liner_tank_yd2 + liner_stone_yd2

      if liner_on_tank and liner_on_stone:
          liner_label = f'WATERTIGHT GEOMEMBRANE LINER (MIN. 30 MIL) \u2014 TANK + STONE ENVELOPE  [{liner_tank_yd2} SY TANK + {liner_stone_yd2} SY STONE]'
      elif liner_on_tank:
          liner_label = f'WATERTIGHT GEOMEMBRANE LINER (MIN. 30 MIL) \u2014 TANK ENVELOPE ONLY  (AQ-100-03.2)'
      elif liner_on_stone:
          liner_label = f'WATERTIGHT GEOMEMBRANE LINER (MIN. 30 MIL) \u2014 STONE BACKFILL ENVELOPE  (AQ-100-03.4)'
      else:
          liner_label = 'WATERTIGHT GEOMEMBRANE LINER (MIN. 30 MIL) \u2014 NOT SPECIFIED'

      # ── Liner protection non-woven (AQ-100-03.4 Note C) ────────────
      # The WT geomembrane requires non-woven on BOTH faces. The standard
      # tank wrap (Line A) / backfill envelope (Line B) already provides one
      # face, so each selected liner envelope adds exactly ONE matching
      # protection layer equal to that liner's yd^2 (waste already included
      # in liner_tank_yd2 / liner_stone_yd2). Mirrors the dashboard JS
      # (index.html: protYd2 = formLinerTankQty + formLinerStoneQty).
      liner_prot_yd2 = liner_tank_yd2 + liner_stone_yd2
      if liner_on_tank and liner_on_stone:
          liner_prot_label = (f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste}% WASTE '
                              f'(LINER PROTECTION \u2014 TANK + STONE FACES, AQ-100-03.4)')
      elif liner_on_tank:
          liner_prot_label = (f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste}% WASTE '
                              f'(LINER PROTECTION \u2014 TANK ENVELOPE FACE, AQ-100-03.4)')
      elif liner_on_stone:
          liner_prot_label = (f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste}% WASTE '
                              f'(LINER PROTECTION \u2014 STONE ENVELOPE FACE, AQ-100-03.4)')
      else:
          liner_prot_label = (f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste}% WASTE '
                              f'(LINER PROTECTION \u2014 AQ-100-03.4)')

      # ── Large diameter pipe connection (line F) ────────────────────
      _lp18   = int(request.form.get('large_pipe_18',   0) or 0)
      _lp24   = int(request.form.get('large_pipe_24',   0) or 0)
      _lp36   = int(request.form.get('large_pipe_36',   0) or 0)
      _lpgt36 = int(request.form.get('large_pipe_gt36', 0) or 0)
      large_pipe_qty  = _lp18 + _lp24 + _lp36 + _lpgt36 or int(request.form.get('large_pipe_qty', 0) or 0)
      large_pipe_desc = 'LARGE DIAMETER PIPE CONNECTION (18\u201336\u2033) \u2014 GEOTEXTILE BOOT / ABUTMENT'
      if config == 'SC':
          _q_layer_heights = [1.394, 2.707, 4.019, 5.331, 6.644, 7.956, 9.268, 10.581]
          _q_void_ratio    = 0.95486
      else:
          _q_layer_heights = [1.509, 3.018, 4.528, 6.037, 7.546, 9.055, 10.564, 12.073]
          _q_void_ratio    = 0.92633
      _q_ptrow_ht  = _q_layer_heights[0]
      _q_ptrow_vol = MODULE_WID * MODULE_LEN * _q_ptrow_ht * _q_void_ratio

      ptrow_enabled_q = request.form.get('ptrow_enabled', '0') == '1'
      ptrow_method_q  = request.form.get('ptrow_method', 'volume')
      ptrow_total_q   = 0

      if ptrow_method_q == 'flow':
          fi = 0
          while f'ptrow_flow_cfs_{fi}' in request.form:
              cfs = float(request.form.get(f'ptrow_flow_cfs_{fi}', 0) or 0)
              ptrow_total_q += math.ceil(cfs / 0.464) if cfs > 0 else 0
              fi += 1
      else:
          idx = 0
          while f'ptrow_wqv_{idx}' in request.form:
              wqv = float(request.form.get(f'ptrow_wqv_{idx}', 0) or 0)
              pct = float(request.form.get(f'ptrow_pct_{idx}', 10) or 10)
              ptrow_total_q += math.ceil((wqv * pct / 100.0) / _q_ptrow_vol) if _q_ptrow_vol > 0 else 0
              idx += 1

      _ptrow_fab_w   = MODULE_WID + 2*_q_ptrow_ht + 2*1.5
      _ptrow_fab_l   = (ptrow_total_q * MODULE_LEN) + 2*1.5
      ptrow_woven_q  = math.ceil(_ptrow_fab_w * _ptrow_fab_l * (1 + geoWaste/100.0) / 9) if ptrow_enabled_q and ptrow_total_q > 0 else 0

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

      # Logo box (top-left) + "An Orbia business." tagline below
      if logo_path and os.path.exists(logo_path):
          try:
              img = ImageReader(logo_path)
              c.drawImage(img, LQ, y - 44, width=130, height=44,
                          preserveAspectRatio=True, mask='auto')
          except Exception:
              pass
      q_text(LQ + 4, y - 52, 'An Orbia business.', 'Helvetica-Oblique', 7, GRAY)

      # "MATERIALS QUOTE" title (center)
      q_text(W/2, y - 14, 'MATERIALS QUOTE', 'Helvetica-Bold', 20, QNY, 'center')

      # Project number box (top-right) — no "An Orbia business." here
      q_rect(W - RQ - 140, y - 52, 140, 52, QLGY)
      q_text(W - RQ - 70, y - 20, project_num if project_num else '—', 'Helvetica-Bold', 18, QNY, 'center')

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
      q_text(W/2, y - 20, 'PREPARED BY:', 'Helvetica-Bold', 8, QNY)
      q_text(W/2 + 90, y - 20, estimator or '—', 'Helvetica', 8, BLACK)

      # Row 3
      q_text(LQ, y - 32, 'CITY:', 'Helvetica-Bold', 8, QNY)
      city_str = location.split(',')[0].strip() if location else '—'
      q_text(LQ + 48, y - 32, city_str, 'Helvetica', 8, BLACK)
      q_text(W/2, y - 32, 'EMAIL:', 'Helvetica-Bold', 8, QNY)
      q_text(W/2 + 50, y - 32, estimator_email or '—', 'Helvetica', 8, BLACK)

      # Row 4
      q_text(LQ, y - 44, 'STATE:', 'Helvetica-Bold', 8, QNY)
      state_str = location.split(',')[1].strip() if location and ',' in location else '—'
      q_text(LQ + 48, y - 44, state_str, 'Helvetica', 8, BLACK)

      # Row 5 — full-width Site Address (requested by CS for ordering/shipping)
      q_text(LQ, y - 56, 'SITE ADDRESS:', 'Helvetica-Bold', 8, QNY)
      q_text(LQ + 78, y - 56, site_address or '—', 'Helvetica', 8, BLACK)

      y -= 66
      q_rule(y)
      y -= 4

      # ── Notes banner (royal blue / white) ───────────────────────
      if project_notes.strip():
          q_rect(LQ, y - 14, QW, 14, QNY)
          q_text(W/2, y - 10, project_notes[:120], 'Helvetica-Bold', 7, WHITE, 'center')
          y -= 18

      # ── Spec summary row (Plans Dated removed) ───────────────────
      q_rect(LQ, y - 28, QW, 28, QLGY)
      col_w = QW / 4

      specs = [
          ('AREA',        f'{tank_area:,.0f} ft²'),
          ('PERIMETER',   f'{round(used_perimeter,1)} ft'),
          ('TANK HEIGHT', f'{round(tank_height,1)} ft'),
          ('MODEL TYPE',  config_label),
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
          ('6', '3091506',    'AQUACELL BASE UNITS \u2014 CONTINGENCY (PRICED AT SAME RATE)', contingency_units, 'EACH', ''),
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
      # Pricing note — clean, no internal pricing exposed
      pricing_note = f'AquaCell system pricing based on project quantities and current pricing.'
      q_text(LQ + 4, y - 9, pricing_note, 'Helvetica-Oblique', 7, GRAY)

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

      alpha = ['A','B','C','D','E','F','G','H','I','J']
      others_rows = [
          (nw_tank_label, geoTank_yd2_adj, 'SQ YD'),
          (f'NON-WOVEN GEOTEXTILE (MIN. 6 OZ./YD\u00b2) + {geoWaste}% WASTE (BACKFILL ONLY)',
           int(geoStone_yd2), 'SQ YD'),
          (f'WOVEN MONOFILAMENT GEOTEXTILE \u2014 PT-ROW\u2122 PRE-TREATMENT + {geoWaste}% WASTE',
           int(ptrow_woven_q), 'SQ YD'),
          (f'BIAXIAL GEOGRID (INTEGRALLY FORMED POLYPROPYLENE) + {geoWaste}% WASTE'
           + (f'  [TOP: {geogrid_top_yd2} SY + BOTTOM: {geogrid_bottom_yd2} SY]'
              if geogrid_top_yd2 > 0 and geogrid_bottom_yd2 > 0 else ''),
           geogrid_total_yd2, 'SQ YD'),
          ('CASTINGS FOR VENTING / INSPECTION PORTS / INLETS',
           top_adapters_12 + top_adapters_16, 'EACH'),
          (large_pipe_desc, large_pipe_qty, 'EACH'),
          ('STONE BACKFILL OR SELECT BACKFILL ESTIMATED FOR UG SYSTEM + 10%',
           int(stone_yd3), 'CU YD'),
          (liner_label, liner_total_yd2, 'SQ YD'),
      ]
      # Liner protection non-woven — only when a liner is actually specified.
      # Sits directly after Line H so the geomembrane and its required
      # protection fabric read as a pair on the quote.
      if liner_prot_yd2 > 0:
          others_rows.append((liner_prot_label, liner_prot_yd2, 'SQ YD'))
      if vs['active']:
          others_rows.append((
              'VOID FILL \u2014 BY OTHERS (NATIVE, STONE, OR MIX \u2014 NO STORAGE CREDIT)',
              int(round(void_fill_yd3)), 'CU YD'))

      def _row_letter(idx):
          # A..Z, then AA, AB, ... — never IndexErrors as rows are added.
          if idx < 26:
              return chr(ord('A') + idx)
          return chr(ord('A') + idx // 26 - 1) + chr(ord('A') + idx % 26)

      for i, (ds, qt, un) in enumerate(others_rows):
          shade = QLGY if i % 2 == 0 else WHITE
          rh = 20
          q_rect(LQ, y - rh, QW, rh, shade)
          q_text(col_ln, y - 10, _row_letter(i), 'Helvetica-Bold', 7.5, QNY)
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
          ('AQUACELL SUB-TOTAL',     money(subtotal),            QNY,   QLGY),
          ('ESTIMATED TAXES*',       '$0.00  (TBD at purchase)', GRAY,  WHITE),
          (f'ESTIMATED FREIGHT* ({freight_pct:.1f}%)', money(freight_cost), colors.HexColor('#784212'), colors.HexColor('#fef9e7')),
          ('ESTIMATED TOTAL',        money(total_with_freight),  QGRN,  colors.HexColor('#eafaf1')),
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
      safe_name    = (project_name or 'Quote').strip().replace(' ', '_')
      safe_num     = (project_num or '').strip().replace(' ', '_')
      name_parts   = '_'.join(filter(None, [safe_num, safe_name]))
      return send_file(buffer, as_attachment=True,
                       download_name=f'AquaCell_Quote_{name_parts}_{datetime.datetime.now().strftime("%m%d%Y")}.pdf',
                       mimetype='application/pdf')

  except Exception as e:
      import traceback
      tb = traceback.format_exc()
      return f'<pre style="color:red;padding:20px">{tb}</pre>', 500


# ══════════════════════════════════════════════════════════════════
#  ROUTE: /download_price_csv  —  single-tank line-item pricing CSV
# ══════════════════════════════════════════════════════════════════
@app.route('/download_price_csv', methods=['POST'])
def download_price_csv():
    try:
        import csv, io as _io

        project_name    = request.form.get('project_name', '')
        project_num     = request.form.get('project_num', '')
        client          = request.form.get('client', '')
        location        = request.form.get('location', '')
        estimator       = request.form.get('estimator', '')
        estimator_email = request.form.get('estimator_email', '')
        generated_str   = datetime.datetime.now().strftime('%m/%d/%Y')

        p_base      = float(request.form.get('up_base',      42.47))
        p_side      = float(request.form.get('up_side',      21.31))
        p_bottom    = float(request.form.get('up_bottom',    18.71))
        p_pipe      = float(request.form.get('up_pipe',      28.53))
        p_adapter12 = float(request.form.get('up_adapter12', 96.54))
        p_adapter16 = float(request.form.get('up_adapter16', 170.60))
        markup_pct  = float(request.form.get('markup_pct', 0) or 0)
        markup_mult = 1 / (1 - markup_pct / 100) if markup_pct < 100 else 1

        freight_pct = float(request.form.get('freight_pct', 0) or 0)

        base_units       = int(request.form.get('bom_base_units',    0) or 0)
        side_plates      = int(request.form.get('bom_side_plates',   0) or 0)
        bottom_plates    = int(request.form.get('bom_bottom_plates', 0) or 0)
        pipe_connectors  = int(request.form.get('bom_pipe_conn',     0) or 0)
        top_adapters_12  = int(request.form.get('bom_top12',         0) or 0)
        top_adapters_16  = int(request.form.get('bom_top16',         0) or 0)
        contingency_units= int(request.form.get('contingency_units', 0) or 0)

        def sell(u): return u * markup_mult

        rows = [
            ('1', '3091506',    'AquaCell Base Unit',               base_units,       'EACH', p_base),
            ('2', '2476600003', 'AquaCell Side Plate',              side_plates,      'EACH', p_side),
            ('3', '2476600001', 'AquaCell Bottom Plate',            bottom_plates,    'EACH', p_bottom),
            ('4', '2476631200', 'AquaCell 8-12" Pipe Connector',   pipe_connectors,  'EACH', p_pipe),
            ('5', '3085857',    'AquaCell Top Connector (12")',    top_adapters_12,  'EACH', p_adapter12),
            ('5', '2476842000', 'AquaCell Top Connector (16")',    top_adapters_16,  'EACH', p_adapter16),
            ('6', '3091506',    'AquaCell Base Unit - Contingency', contingency_units,'EACH', p_base),
        ]

        out = _io.StringIO()
        w   = csv.writer(out)

        w.writerow(['AquaCell Order Pricing Export'])
        w.writerow(['Generated', generated_str])
        w.writerow(['Project Name', project_name])
        w.writerow(['Project #',    project_num])
        w.writerow(['Client',       client])
        w.writerow(['Location',     location])
        w.writerow(['Estimator',    estimator])
        w.writerow(['Email',        estimator_email])
        w.writerow([])
        w.writerow(['Line', 'Part #', 'Description', 'Qty', 'Unit',
                    'Floor Unit Price', 'Sell Unit Price', 'Extended Floor', 'Extended Sell'])

        for ln, part, desc, qty, unit, unit_floor in rows:
            if qty == 0:
                continue
            unit_sell = sell(unit_floor)
            w.writerow([ln, part, desc, qty, unit,
                        f'${unit_floor:,.2f}', f'${unit_sell:,.2f}',
                        f'${qty * unit_floor:,.2f}', f'${qty * unit_sell:,.2f}'])

        total_floor = sum(r[5] * r[3] for r in rows if r[3] > 0)
        total_sell  = sum(sell(r[5]) * r[3] for r in rows if r[3] > 0)
        freight_est = total_sell * freight_pct / 100
        w.writerow([])
        w.writerow(['', '', '', '', 'TOTAL FLOOR COST', '', '', f'${total_floor:,.2f}', ''])
        w.writerow(['', '', '', '', 'TOTAL SELL PRICE', '', '', '', f'${total_sell:,.2f}'])
        w.writerow(['', '', '', '', f'ESTIMATED FREIGHT ({freight_pct:.1f}%)', '', '', '', f'${freight_est:,.2f}'])

        out.seek(0)
        buf = io.BytesIO(out.getvalue().encode('utf-8'))
        safe_name  = (project_name or 'Quote').strip().replace(' ', '_')
        safe_num   = (project_num or '').strip().replace(' ', '_')
        name_parts = '_'.join(filter(None, [safe_num, safe_name]))
        return send_file(buf, as_attachment=True,
                         download_name=f'AquaCell_PriceExport_{name_parts}_{datetime.datetime.now().strftime("%m%d%Y")}.csv',
                         mimetype='text/csv')

    except Exception as e:
        import traceback
        return f'<pre style="color:red;padding:20px">{traceback.format_exc()}</pre>', 500


# ══════════════════════════════════════════════════════════════════
#  DETAIL SHEET EXPORTER — GitHub file listing (cached + authenticated)
# ══════════════════════════════════════════════════════════════════

def _github_headers():
    """Build request headers, injecting PAT when available."""
    h = {
        "Accept":     "application/vnd.github.v3+json",
        "User-Agent": "AquaCell-Calculator"
    }
    if GITHUB_PAT:
        h["Authorization"] = f"Bearer {GITHUB_PAT}"
    return h

@app.route('/api/details')
def api_details():
    """Return a cached, sorted list of PDF filenames from the GitHub Details folder."""
    import time
    global _details_cache, _details_cache_time

    # Serve from cache if still fresh
    if _details_cache and (time.time() - _details_cache_time) < _CACHE_TTL_SECONDS:
        return jsonify({"files": _details_cache, "cached": True})

    url = (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/contents/{GITHUB_FOLDER}?ref={GITHUB_BRANCH}"
    )
    try:
        resp = requests.get(url, timeout=12, headers=_github_headers())
        resp.raise_for_status()
        files = sorted(
            f["name"] for f in resp.json()
            if isinstance(f, dict) and f.get("name", "").lower().endswith(".pdf")
        )
        _details_cache      = files
        _details_cache_time = time.time()
        return jsonify({"files": files, "cached": False})
    except Exception as exc:
        # Return stale cache rather than an error if we have one
        if _details_cache:
            return jsonify({"files": _details_cache, "cached": True, "warning": str(exc)})
        return jsonify({"error": str(exc), "files": []}), 500


# ══════════════════════════════════════════════════════════════════
#  DETAIL SHEET EXPORTER — merge selected PDFs into one download
# ══════════════════════════════════════════════════════════════════

@app.route('/download_details_pdf', methods=['POST'])
def download_details_pdf():
    """Fetch selected PDFs from GitHub raw and merge into a single PDF."""
    try:
        data     = request.get_json(force=True)
        selected = data.get('files', [])
        if not selected:
            return jsonify({"error": "No files selected"}), 400

        base_raw = (
            f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
            f"/{GITHUB_BRANCH}/{GITHUB_FOLDER}/"
        )

        writer = PdfWriter()
        for filename in selected:
            raw_url = base_raw + filename
            r = requests.get(
                raw_url, timeout=30,
                headers=_github_headers()
            )
            r.raise_for_status()
            reader = PyPdfReader(BytesIO(r.content))
            for page in reader.pages:
                writer.add_page(page)

        output = BytesIO()
        writer.write(output)
        output.seek(0)

        date_str = datetime.datetime.now().strftime("%m%d%Y")
        return send_file(
            output,
            as_attachment=True,
            download_name=f"AquaCell_Details_{date_str}.pdf",
            mimetype="application/pdf"
        )
    except Exception as exc:
        import traceback
        return jsonify({"error": str(exc), "trace": traceback.format_exc()}), 500


# ══════════════════════════════════════════════════════════════════
#  CLIENT-FACING DOCUMENT LIBRARY — Design Tools "Document Library" tab
#
#  Deliberately narrower, allowlisted subset of the Details/ folder for
#  the external client build. Separate from the internal Detail Sheet
#  Exporter above (/api/details, /download_details_pdf) — that one stays
#  unrestricted for internal estimators. Excludes warranty, CSI spec,
#  marketing trifold, and the not-yet-updated civil sheet doc, per
#  James's direction 2026-08-17. Every route here validates requested
#  filenames against this allowlist server-side — the UI only ever
#  shows this set, but a direct API call must be blocked from reaching
#  anything outside it too.
# ══════════════════════════════════════════════════════════════════
_CLIENT_DOC_CATEGORIES = ['Spec / Detail Sheets', 'Cross Sections', 'Installation Guides']

_CLIENT_DOC_LIBRARY = [
    # Spec / Detail Sheets
    ('AQ-100-01.4 AquaCell Acceptable Backfill Materials Table-Layout1.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-02.5 AquaCell Minimum Load Requirements-Layout.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-03.1.3 AquaCell Infiltration Detail-Layout1.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-03.2.3 AquaCell Detention Detail-LINER ONLY.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-03.2.4 AquaCell Detention Detail-Layout1.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-03a.1 AquaCell Detention Detail with Underdrain-Layout1.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-04.3 AquaCell Top Adapter Connection Detail-Layout1.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-05.2 AquaCell 1-5 Layer Stack w. Measurements.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-06.2 AquaCell Vertical Riser Connection Detail (HS-20)-Layout1.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-06.3 AquaCell Vertical Riser Connection Detail (Non-traffic)-Layout1.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-07.2 AquaCell Connections by Pipe Type -Layout1.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-08.2 AquaCell Cover & Burial Depths - SC & EX Models.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-09 AquaCell Pallet Stack Dimensions.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-10.2 AquaCell Large Diameter Pipe Abutment-Layout 2.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-19 Sediment Bag Installation from a Grated Inlet into AquaCell.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-20 Construction Loads Over AquaCell System.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-21.2 AquaCell with Permeable Block Surface - Layout1.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-22 Six-inch PVC Pipe to AquaCell Side Panel Detail.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-23 AquaCell Extra Strong (EX) Crate Measurements-Layout.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-24 AquaCell Min. Pipe Installation Distance from Manhole.pdf', 'Spec / Detail Sheets'),
    ('AQ-100-26 Roof Drain Connection to AquaCell Layout1 (1).pdf', 'Spec / Detail Sheets'),
    ('AQ-100-27 AquaCell System with Underdrain 1.30.26.pdf', 'Spec / Detail Sheets'),
    ('AQ-200-01.4 AquaCell Volume Calculations.pdf', 'Spec / Detail Sheets'),
    ('AQ-200-02 AquaCell Compatible Pipe Connections.pdf', 'Spec / Detail Sheets'),
    ('AQ-300-01.1 8 IN. - 12 IN.AQUACELL PIPE CONNECTOR DETAIL.pdf', 'Spec / Detail Sheets'),
    ('AQ-300-02.1 12 IN. AQUACELL TOP ADAPTER DETAIL-Layout.pdf', 'Spec / Detail Sheets'),
    ('AQ-300-03.1 16 IN.AQUACELL TOP ADAPTER DETAIL-Layout.pdf', 'Spec / Detail Sheets'),
    ('AQ-300-05.1 AQUACELL SIDE PLATE DETAIL-Layout.pdf', 'Spec / Detail Sheets'),
    ('AQ-300-06.1 AQUACELL BOTTOM PLATE DETAIL-Layout.pdf', 'Spec / Detail Sheets'),
    ('AQ-300-07.1 AQUACELL BASE UNIT DETAIL-Layout.pdf', 'Spec / Detail Sheets'),
    ('AQ-300-08.1 AQUACELL SC FULL UNIT DETAIL-Layout.pdf', 'Spec / Detail Sheets'),
    ('AQ-300-15.1 AQUACELL EX DOUBLE STACK FULL UNIT DETAIL-Layout.pdf', 'Spec / Detail Sheets'),
    # Cross Sections
    ('AQ-100-11.2 AquaCell SC-1 Layer Cross Section.pdf', 'Cross Sections'),
    ('AQ-100-12.2 AquaCell SC-2 Layer Cross Section.pdf', 'Cross Sections'),
    ('AQ-100-13.2 AquaCell SC-3 Layer Cross Section.pdf', 'Cross Sections'),
    ('AQ-100-14.2 AquaCell SC-4 Layer Cross Section.pdf', 'Cross Sections'),
    ('AQ-100-15.2 AquaCell SC-5 Layer Cross Section.pdf', 'Cross Sections'),
    ('AQ-100-16.1 AquaCell SD-6 Layer Cross Section Layout1 (1).pdf', 'Cross Sections'),
    ('AQ-100-17.2 AquaCell SC-7 Layer Cross Section.pdf', 'Cross Sections'),
    ('AQ-100-18.2 AquaCell SC-8 Layer Cross Section.pdf', 'Cross Sections'),
    ('AquaCell SC-1 thru SC-8 Cross Sections 12.12.25.pdf', 'Cross Sections'),
    # Installation Guides
    ('AQ-100-25.3 AquaCell PT-ROW™ Installation Guidance.pdf', 'Installation Guides'),
    ('Loading and Unloading Guide for Wavin AquaCell Stormwater Products V.1.pdf', 'Installation Guides'),
    ('PT-ROW™ Technical Note - Woven Fabric 9.26.25.pdf', 'Installation Guides'),
    ('Wavin AquaCell PT-ROW™ Sizing and Design Guidance.pdf', 'Installation Guides'),
    ('Wavin Standard Maintenance Recommendation Letter 1.19.26.pdf', 'Installation Guides'),
    ('WA_Aquacell-Installation-Guide_digital_10.25.pdf', 'Installation Guides'),
]
_CLIENT_DOC_FILENAMES = {f for f, _ in _CLIENT_DOC_LIBRARY}


def _client_doc_friendly_name(filename):
    name = re.sub(r'\.pdf$', '', filename, flags=re.IGNORECASE)
    name = re.sub(r'-?\s*Layout\d*(\s*\(\d+\))?$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s{2,}', ' ', name).strip()
    return name


@app.route('/api/client-details')
def api_client_details():
    """Curated, categorized document list for the client-facing library tab."""
    groups = []
    for cat in _CLIENT_DOC_CATEGORIES:
        files = [{'filename': f, 'label': _client_doc_friendly_name(f)}
                 for f, c in _CLIENT_DOC_LIBRARY if c == cat]
        if files:
            groups.append({'category': cat, 'files': files})
    return jsonify({'groups': groups})


@app.route('/api/client-details/view/<path:filename>')
def api_client_details_view(filename):
    """Stream one allowlisted PDF inline (for the in-page viewer)."""
    if filename not in _CLIENT_DOC_FILENAMES:
        return jsonify({'error': 'Document not found.'}), 404
    raw_url = (
        f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/{GITHUB_BRANCH}/{GITHUB_FOLDER}/{filename}"
    )
    try:
        r = requests.get(raw_url, timeout=20, headers=_github_headers())
        r.raise_for_status()
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502
    return send_file(BytesIO(r.content), mimetype='application/pdf',
                      as_attachment=False, download_name=filename)


@app.route('/api/client-details/download', methods=['POST'])
def api_client_details_download():
    """Merge selected allowlisted PDFs into one download."""
    try:
        data = request.get_json(force=True)
        selected = data.get('files', [])
        if not selected:
            return jsonify({'error': 'No files selected'}), 400
        invalid = [f for f in selected if f not in _CLIENT_DOC_FILENAMES]
        if invalid:
            return jsonify({'error': f'Unknown document(s): {", ".join(invalid)}'}), 400

        base_raw = (
            f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
            f"/{GITHUB_BRANCH}/{GITHUB_FOLDER}/"
        )
        writer = PdfWriter()
        for filename in selected:
            r = requests.get(base_raw + filename, timeout=30, headers=_github_headers())
            r.raise_for_status()
            reader = PyPdfReader(BytesIO(r.content))
            for page in reader.pages:
                writer.add_page(page)

        output = BytesIO()
        writer.write(output)
        output.seek(0)
        date_str = datetime.datetime.now().strftime("%m%d%Y")
        return send_file(
            output,
            as_attachment=True,
            download_name=f"AquaCell_Document_Library_{date_str}.pdf",
            mimetype="application/pdf"
        )
    except Exception as exc:
        import traceback
        return jsonify({"error": str(exc), "trace": traceback.format_exc()}), 500


_CLIENT_CAD_ZIPS = [
    ('AquaCell SC-1 thru SC-8 Layer Cross Sections.zip', 'Cross Sections (8 DWG files)'),
    ('AquaCell Standard Details_August_2026.zip', 'Standard Details (DWG files)'),
]
_CLIENT_CAD_ZIP_FILENAMES = {f for f, _ in _CLIENT_CAD_ZIPS}


@app.route('/api/client-details/cad-zips')
def api_client_cad_zips():
    return jsonify({'files': [{'filename': f, 'label': label} for f, label in _CLIENT_CAD_ZIPS]})


@app.route('/api/client-details/cad-zip/<path:filename>')
def api_client_cad_zip_download(filename):
    """Passthrough download of an allowlisted CAD source ZIP."""
    if filename not in _CLIENT_CAD_ZIP_FILENAMES:
        return jsonify({'error': 'File not found.'}), 404
    raw_url = (
        f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/{GITHUB_BRANCH}/{GITHUB_FOLDER}/{filename}"
    )
    try:
        r = requests.get(raw_url, timeout=60, headers=_github_headers())
        r.raise_for_status()
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502
    return send_file(BytesIO(r.content), mimetype='application/zip',
                      as_attachment=True, download_name=filename)


if __name__ == '__main__':
    app.run(debug=True)



