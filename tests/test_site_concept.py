# Site Concept Builder (Step 1 backend) — Flask test-client checks.
# Run from the repo root:  python3 tests/test_site_concept.py
import sys, random, math
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
W, L = A.MODULE_WID, A.MODULE_LEN
c = A.app.test_client()
def post(inputs):
    r = c.post('/design-tools/calculate', json={'calc_type':'site_concept','inputs':inputs})
    return r.status_code, r.get_json()
fails = 0
def check(name, cond, info=''):
    global fails
    print(('PASS ' if cond else 'FAIL ') + name, info if not cond else '')
    if not cond: fails += 1
close = lambda a,b,tol=0.05: abs(a-b) <= tol

# 1. Rectangle vs calc_tank rectangle path (multi-tank per-tank calc)
for cfg in ('SC','EX'):
  for layers in (1,3,8):
    nw, nl, d = 10, 20, 1.0
    code, j = post({'tanks':[{'config':cfg,'layers':layers,'mode':'rows',
                  'rows':[{'crate_count':nw,'offset_crates':0}]*nl,'perimeter_stone_width':d}]})
    t = j['tanks'][0]
    ct = A.calc_tank({'config':cfg,'layers':layers,'known_width':nw*W+1e-6,'known_length':nl*L+1e-6,
                      'perimeter_stone_width':d,'cover_stone':1.0,'base_stone':0.333,'stone_void':0.40,'geoWaste':20})
    tag=f'rect {cfg} L{layers}'
    check(tag+' http200', code==200)
    check(tag+' excav area', close(t['excavation_area_sf'], (nw*W+2*d)*(nl*L+2*d)), t['excavation_area_sf'])
    check(tag+' excav perim', close(t['excavation_perimeter_ft'], 2*(nw*W+nl*L)+8*d))
    check(tag+' crates', t['num_crates']==ct['num_crates'], (t['num_crates'],ct['num_crates']))
    check(tag+' tank storage', close(t['tank_storage_cf'], ct['tank_storage'],0.1), (t['tank_storage_cf'],ct['tank_storage']))
    check(tag+' stone storage', close(t['stone_storage_cf'], ct['stone_storage'],0.2), (t['stone_storage_cf'],ct['stone_storage']))
    check(tag+' total storage', close(t['total_storage_cf'], ct['total_storage'],0.2))
    check(tag+' side plates', t['bom']['side_plates']['qty']==ct['side_plates'], (t['bom']['side_plates'],ct['side_plates']))
    check(tag+' base units', t['bom']['base_units']['qty']==ct['base_units'])
    check(tag+' bottom plates', t['bom']['bottom_plates']['qty']==ct['bottom_plates'])
    check(tag+' recommended contingency == calc_tank contingency', t['contingency_recommended']['qty']==ct['contingency'])
    check(tag+' stone tons', close(t['stone_tons'], ct['stone_tons']))
    check(tag+' stone yd3', close(t['stone_yd3'], ct['stone_yd3']))

# 2. d = 0 → excavation == tank (0 must be honoured, not defaulted)
code,j = post({'tanks':[{'config':'SC','layers':2,'rows':[{'crate_count':5,'offset_crates':0},{'crate_count':3,'offset_crates':2}],
               'perimeter_stone_width':0,'cover_stone':0,'base_stone':0}]})
t=j['tanks'][0]
check('d=0 excav area = tank area', close(t['excavation_area_sf'], t['tank_footprint_sf']))
check('d=0 excav perim = tank perim', close(t['excavation_perimeter_ft'], t['tank_perimeter_ft']))
check('cover/base 0 honoured', t['cover_stone_ft']==0 and t['base_stone_ft']==0 and close(t['stone_storage_cf'],0))

# 3. Brute-force raster check of Option A offset on random shapes
def raster(rows, d, res=0.02):
    rects=[(o*W-d, i*L-d, (o+n)*W+d, (i+1)*L+d) for i,(n,o) in enumerate(rows)]
    xmin=min(r[0] for r in rects); ymin=min(r[1] for r in rects)
    xmax=max(r[2] for r in rects); ymax=max(r[3] for r in rects)
    nx=int(round((xmax-xmin)/res)); ny=int(round((ymax-ymin)/res))
    area=0
    for jy in range(ny):
        y=ymin+(jy+0.5)*res
        for ix in range(nx):
            x=xmin+(ix+0.5)*res
            if any(r[0]<x<r[2] and r[1]<y<r[3] for r in rects): area+=1
    return area*res*res
random.seed(7)
for k in range(6):
    n=random.randint(2,5); rows=[(random.randint(1,6), random.randint(0,4)) for _ in range(n)]
    d=random.choice([0.5,1.0,2.5])
    code,j=post({'tanks':[{'config':'SC','layers':1,'rows':[{'crate_count':a,'offset_crates':b} for a,b in rows],'perimeter_stone_width':d}]})
    if code!=200: check(f'random {k}', False, j); continue
    t=j['tanks'][0]; ra=raster(rows,d)
    check(f'random shape {k} area vs raster {rows} d={d}', abs(t['excavation_area_sf']-ra)/ra < 0.01, (t['excavation_area_sf'], round(ra,2)))

# 4. L-shape (no narrow notch) matches A + P·d + 4d²
rows=[{'crate_count':10,'offset_crates':0}]*3+[{'crate_count':4,'offset_crates':0}]*5
code,j=post({'tanks':[{'rows':rows,'perimeter_stone_width':1.0}]}); t=j['tanks'][0]
check('L-shape A+Pd+4d²', close(t['excavation_area_sf'], t['tank_footprint_sf']+t['tank_perimeter_ft']*1+4))
check('L-shape P+8d', close(t['excavation_perimeter_ft'], t['tank_perimeter_ft']+8))

# 5. Narrow notch fills in: C-shape, notch 1 row (3.937 ft) tall, d=2.5 → 2d=5 > 3.937
rows=[{'crate_count':10,'offset_crates':0},{'crate_count':2,'offset_crates':0},{'crate_count':10,'offset_crates':0}]
code,j=post({'tanks':[{'rows':rows,'perimeter_stone_width':2.5}]}); t=j['tanks'][0]
check('narrow notch dug out → equals bbox excavation', close(t['excavation_area_sf'], (10*W+5)*(3*L+5)), t['excavation_area_sf'])

# 6. Multi-tank totals
code,j=post({'tanks':[{'label':'A','rows':[{'crate_count':5,'offset_crates':0}]*4},{'rows':[{'crate_count':3,'offset_crates':0}]*2,'config':'EX','layers':3}]})
check('two tanks 200', code==200)
check('default label Tank 2', j['tanks'][1]['label']=='Tank 2')
check('totals crates', j['totals']['num_crates']==sum(t['num_crates'] for t in j['tanks']))
check('totals base units', j['totals']['bom']['base_units']['qty']==sum(t['bom']['base_units']['qty'] for t in j['tanks']))

# 7. Warnings & errors
code,j=post({'tanks':[{'rows':[{'crate_count':2,'offset_crates':0},{'crate_count':2,'offset_crates':5}]}]})
check('disconnected rows warn', code==200 and len(j['tanks'][0]['warnings'])==1)
code,j=post({'tanks':[]}); check('no tanks → 400', code==400)
code,j=post({'tanks':[{'rows':[]}]}); check('no rows → 400', code==400 and 'Tank 1' in j['error'])
code,j=post({'tanks':[{'rows':[{'crate_count':2,'offset_crates':0}],'stone_void':40}]}); check('void as percent → 400', code==400)
code,j=post({'tanks':[{'rows':[{'crate_count':2,'offset_crates':0}],'perimeter_stone_width':-1}]}); check('negative → 400', code==400)
code,j=post({'tanks':[{'rows':[{'crate_count':2,'offset_crates':0}],'cover_stone':'abc'}]}); check('non-numeric → 400', code==400)
# envelope mode
code,j=post({'tanks':[{'mode':'envelope','envelope_width_ft':40,'envelope_length_ft':80,'perimeter_stone_width':1}]})
t=j['tanks'][0]; check('envelope snaps down', code==200 and t['bounding_width_ft']<=40 and t['bounding_length_ft']<=80, t.get('bounding_width_ft'))

# 8. Internal-only gate
A.PRICING_ENABLED=False
code,j=post({'tanks':[{'rows':[{'crate_count':2,'offset_crates':0}]}]}); check('BETA build → 404', code==404, code)
A.PRICING_ENABLED=True
# regression: existing calc still works
r=c.post('/design-tools/calculate', json={'calc_type':'complex_shape_builder','inputs':{'rows':[{'crate_count':3,'offset_crates':0}]}})
check('complex_shape_builder unchanged 200', r.status_code==200)
check('design-tools page 200', c.get('/design-tools').status_code==200)
# 9. tank perimeter agrees with Complex Shape Builder perimeter on random shapes
random.seed(3); bad=0
for k in range(200):
    rows=[{'crate_count':random.randint(1,8),'offset_crates':random.randint(0,6)} for _ in range(random.randint(1,8))]
    g=A.calc_complex_shape_builder({'rows':rows}); code,j=post({'tanks':[{'rows':rows}]})
    if not close(g['perimeter_ft'], j['tanks'][0]['tank_perimeter_ft'], 0.011): bad+=1
check('perimeter matches builder on 200 random shapes', bad==0, bad)

# 10. PR #41 review fixes
# (a) "long axis across width" dropped: any orientation sent is ignored
for orient in ('len_along_width', 'bogus', None):
    tk={'mode':'envelope','envelope_width_ft':3.937001,'envelope_length_ft':5.905501,
        'perimeter_stone_width':0,'cover_stone':0,'base_stone':0}
    if orient: tk['orientation']=orient
    code,j=post({'tanks':[tk]}); t=j['tanks'][0]
    check(f'orientation {orient!r} ignored -> down-length count (2 crates)', code==200 and t['crates_layer']==2, t.get('crates_layer'))
    check(f'orientation {orient!r}: d=0 excav == tank', close(t['excavation_area_sf'], t['tank_footprint_sf'], 0.01))
    check(f'orientation {orient!r}: no negative stone', t['stone_perim_gross_cf'] >= -1e-6)
# (b) project contingency from combined base units
code,j=post({'tanks':[{'rows':[{'crate_count':10,'offset_crates':0}],'layers':1},{'rows':[{'crate_count':10,'offset_crates':0}],'layers':1}]})
check('per-tank contingency 46 each', all(t['contingency_recommended']['qty']==46 for t in j['tanks']))
check('project contingency 36 (not 92)', j['totals']['contingency_recommended']['qty']==36, j['totals']['contingency_recommended'])
# Contingency is a RECOMMENDATION only — never inside the official BOM / totals (James, 2026-10-06)
check('contingency not in per-tank BOM', all('contingency' not in t['bom'] for t in j['tanks']))
check('contingency not in project BOM', 'contingency' not in j['totals']['bom'])
check('project weight excludes contingency',
      abs(j['totals']['total_weight_lbs'] - sum(v['weight_lbs'] for v in j['totals']['bom'].values())) < 0.05
      and j['totals']['total_weight_lbs'] < sum(v['weight_lbs'] for v in j['totals']['bom'].values()) + j['totals']['contingency_recommended']['weight_lbs'])
check('recommended contingency weight = qty x base-unit weight', abs(j['totals']['contingency_recommended']['weight_lbs'] - 36*A._MT_WEIGHTS['base']) < 0.05)
# (c) config normalised / rejected
code,j=post({'tanks':[{'rows':[{'crate_count':4,'offset_crates':0}],'config':'sc'}]})
t=j['tanks'][0]
check("config 'sc' normalised to SC BOM", code==200 and t['config']=='SC' and t['bom']['base_units']['qty']==t['num_crates'] and t['bom']['bottom_plates']['qty']==4)
code,j=post({'tanks':[{'rows':[{'crate_count':4,'offset_crates':0}],'config':'XX'}]})
check('unknown config -> 400', code==400)
# (d) drawing frame starts at x = 0
code,j=post({'tanks':[{'rows':[{'crate_count':3,'offset_crates':5}]}]})
t=j['tanks'][0]
check('offset row normalised to x=0', close(t['tank_rects_ft'][0][0], 0, 1e-6) and close(t['bounding_width_ft'], 3*W, 0.001)
      and t['bounding_width_crates']==3 and close(t['fill_efficiency_pct'],100,0.05), t['tank_rects_ft'])
code,j=post({'tanks':[{'rows':[{'crate_count':3,'offset_crates':5},{'crate_count':2,'offset_crates':6}],'perimeter_stone_width':1}]})
t=j['tanks'][0]
check('min excavation x = -d', close(min(r[0] for r in t['excavation_rects_ft']), -1, 1e-6))

# 11. Step 3 — estimate PDF (/design-tools/download_pdf, calc_type site_concept)
import base64, io
from pypdf import PdfReader
_png = base64.b64encode(bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000d49444154789c6360f8cf00000301010018dd8db40000000049454e44ae426082')).decode()
IMG = 'data:image/png;base64,' + _png
def pdf(inputs, image=IMG, project='Maple Ridge #2'):
    return c.post('/design-tools/download_pdf', json={'calc_type':'site_concept','project_name':project,'image':image,'inputs':inputs})
two = {'tanks':[{'label':'Tank A','rows':[{'crate_count':10,'offset_crates':0}]*5+[{'crate_count':20,'offset_crates':0}]*15},
                {'label':'Tank B','config':'EX','layers':4,'mode':'envelope','envelope_width_ft':30,'envelope_length_ft':60}]}
r = pdf(two)
check('pdf 200 + application/pdf', r.status_code==200 and r.headers['Content-Type']=='application/pdf', r.status_code)
check('pdf filename', 'AquaCell_Site_Concept_Maple_Ridge_2.pdf' in r.headers.get('Content-Disposition',''))
rd = PdfReader(io.BytesIO(r.data))
check('pdf pages = 1 + tanks + 1', len(rd.pages)==4, len(rd.pages))
calc = c.post('/design-tools/calculate', json={'calc_type':'site_concept','inputs':two}).get_json()
txt = ''.join(pg.extract_text() for pg in rd.pages)
check('pdf shows server total storage', f"{calc['totals']['total_storage_cf']:,.1f} cf" in txt, calc['totals']['total_storage_cf'])
for t in calc['tanks']:
    check(f"pdf shows {t['label']} storage + excavation", f"{t['total_storage_cf']:,.1f} cf" in txt and f"{t['excavation_vol_cy']:,.1f} cy" in txt)
last = rd.pages[-1].extract_text()
check('pdf project contingency shown as recommended', f"RECOMMENDED CONTINGENCY (project, combined base units): {calc['totals']['contingency_recommended']['qty']:,} base units" in last, last[-600:])
check('pdf notes recommended amount only', 'recommended amount only' in last and 'NOT included in the quantities or totals' in last)
check('pdf materials total excludes contingency', 'TOTAL (excludes recommended contingency)' in last and 'incl. contingency' not in txt)
check('pdf project weight = official total', f"{calc['totals']['total_weight_lbs']:,.1f}" in last)
for k, t in enumerate(calc['tanks']):
    pg = rd.pages[1 + k].extract_text()
    check(f"pdf tank {k+1} recommended contingency note", f"RECOMMENDED CONTINGENCY (this tank): {t['contingency_recommended']['qty']:,} base units" in pg)
r = pdf(two, image=''); check('pdf without image -> 400', r.status_code==400)
r = pdf({'tanks':[{'rows':[]}]}); check('pdf bad tank -> 400', r.status_code==400)
r = pdf({'tanks':[{'label':'X'*300,'rows':[{'crate_count':3,'offset_crates':0}]}]}, project=None)
check('pdf long label / no project -> 200', r.status_code==200)
many = {'tanks':[{'label':f'T{i}','rows':[{'crate_count':1+i%7,'offset_crates':i%3}]*(1+i%9)} for i in range(25)]}
r = pdf(many); check('pdf 25 tanks -> 27 pages', r.status_code==200 and len(PdfReader(io.BytesIO(r.data)).pages)==27)
A.PRICING_ENABLED=False
r = pdf(two); check('pdf on BETA build -> 404', r.status_code==404)
A.PRICING_ENABLED=True
# 12. Waste / overage labelling, sf fabric, hundredths heights (James's PDF markup, 2026-10-27)
one = {'tanks':[{'label':'SC-2','config':'SC','layers':2,'mode':'rows',
        'rows':[{'crate_count':n,'offset_crates':9-n} for n in range(1,10)],'perimeter_stone_width':1}]}
calc1 = c.post('/design-tools/calculate', json={'calc_type':'site_concept','inputs':one}).get_json()
t1, tot1 = calc1['tanks'][0], calc1['totals']
check('stone overage pct = 10 (tank + totals)', t1['stone_overage_pct']==10 and tot1['stone_overage_pct']==10)
check('stone yd3 = envelope x 1.10 / 27', close(t1['stone_yd3'], t1['stone_env_cf']*1.10/27, 0.06), (t1['stone_yd3'], t1['stone_env_cf']))
check('stone tons = envelope x 1.10 x 100 lb/cf / 2000', close(t1['stone_tons'], t1['stone_env_cf']*1.10*100/2000, 0.06))
check('geotextile sf = yd2 x 9 (tank/stone/total)',
      all(close(t1[f'geotextile_{k}_sf'], t1[f'geotextile_{k}_yd2']*9, 0.5) for k in ('tank','stone','total')))
check('geotextile total = tank + envelope (waste in both)', close(t1['geotextile_total_sf'], t1['geotextile_tank_sf']+t1['geotextile_stone_sf'], 0.11))
check('totals carry sf + waste pcts', tot1['geotextile_total_sf']==round(t1['geotextile_total_sf'],2) and tot1['geo_waste_pcts']==[20.0])
mixed = {'tanks':[dict(one['tanks'][0], geo_waste_pct=10), dict(one['tanks'][0], label='B', geo_waste_pct=20)]}
cm = c.post('/design-tools/calculate', json={'calc_type':'site_concept','inputs':mixed}).get_json()
check('mixed waste pcts listed sorted', cm['totals']['geo_waste_pcts']==[10.0, 20.0], cm['totals'].get('geo_waste_pcts'))
check('waste note: single pct', A._scb_waste_note([20.0])=='incl. 20% waste')
check('waste note: range', A._scb_waste_note([10.0,20.0])=='incl. 10-20% waste (varies by tank)')
rd1 = PdfReader(io.BytesIO(pdf(one).data)); p2 = rd1.pages[1].extract_text(); p3 = rd1.pages[2].extract_text(); p1 = rd1.pages[0].extract_text()
h, dpt = t1['tank_height_ft'], t1['total_system_depth_ft']
check('pdf height to hundredths', f'{h:.2f} ft' in p2 and f'{h:.3f} ft' not in p2, h)
check('pdf system depth to hundredths', f'{dpt:.2f} ft' in p2 and f'{dpt:.3f} ft' not in p2, dpt)
check('pdf stone quantity notes 10% overage', 'Stone Quantity (incl. 10% overage)' in p2, p2)
check('pdf geotextile in sf, no yd2', f"{t1['geotextile_total_sf']:,.1f} sf" in p2 and 'yd²' not in p2 and 'yd²' not in p3)
check('pdf geotextile lines each note waste', p2.count('incl. 20% waste') == 3, p2.count('incl. 20% waste'))
check('pdf p3 stone overage note', 'Stone (all tanks) — incl. 10% overage' in p3, p3)
check('pdf p3 geotextile waste note + sf', 'Geotextile (all tanks) — incl. 20% waste' in p3 and f"{tot1['geotextile_total_sf']:,.1f} sf" in p3)
check('pdf p3 excavation + storage notes', 'net, no overage' in p3 and 'net, no waste/overage' in p3)
check('pdf p1 stone overage note', 'Stone (incl. 10% overage)' in p1)
rdm = PdfReader(io.BytesIO(pdf(mixed).data))
check('pdf p3 mixed waste range', 'incl. 10-20% waste (varies by tank)' in rdm.pages[-1].extract_text())

# 13. calculator_import: the single / multi-tank Complex Shape fields must reproduce the tank
import re
random.seed(11); badn = 0; bad_snap = 0
shapes = [one['tanks'][0]['rows']] + [[{'crate_count':random.randint(1,12),'offset_crates':random.randint(0,6)} for _ in range(random.randint(1,12))] for _ in range(120)]
shapes += [[{'crate_count':17,'offset_crates':0}], [{'crate_count':1,'offset_crates':0}]*13, [{'crate_count':1,'offset_crates':0}]]  # prime / thin
for rows in shapes:
    for cfg, lay in (('SC',1),('EX',3)):
        code, j = post({'tanks':[{'config':cfg,'layers':lay,'rows':rows,'perimeter_stone_width':1.25,'cover_stone':0.8,'base_stone':0.5,'stone_void':0.38,'geo_waste_pct':15}]})
        t = j['tanks'][0]; ci = t['calculator_import']
        mt = A.calc_tank({'config':cfg,'layers':lay,'shape_mode':'complex',
                          'complex_scaled_area':ci['complex_scaled_area'],'complex_known_dim':ci['complex_known_dim'],
                          'complex_tank_perim':ci['complex_tank_perim'],'complex_excav_area':ci['complex_excav_area'],
                          'complex_excav_perim':ci['complex_excav_perim'],'cover_stone':0.8,'base_stone':0.5,
                          'perimeter_stone_width':1.25,'stone_void':0.38,'geoWaste':15})
        ok = (mt['num_crates']==t['num_crates'] and mt['base_units']==t['bom']['base_units']['qty']
              and mt['side_plates']==t['bom']['side_plates']['qty'] and mt['bottom_plates']==t['bom']['bottom_plates']['qty']
              and close(mt['tank_storage'], t['tank_storage_cf'], 0.1)
              # calc_tank rounds snapped width/length to 2 decimals before its perimeter-stone volume -> <= ~0.3% on total
              and abs(mt['total_storage']-t['total_storage_cf']) <= max(3.0, 0.004*t['total_storage_cf'])
              and close(mt['stone_yd3'], t['stone_yd3'], 0.1) and close(mt['geoTank'], t['geotextile_tank_sf'], 0.2)
              and close(mt['geoStone'], t['geotextile_stone_sf'], 0.2))
        if not ok: badn += 1
        if ci['crates_known']*ci['crates_other'] != t['crates_layer']: bad_snap += 1
check('import fields reproduce crates/BOM/storage/stone/fabric via calc_tank (SC+EX, 123 shapes)', badn==0, badn)
check('equivalent grid a x b == crates per layer', bad_snap==0, bad_snap)
# 3-decimal values only (single-tank inputs use step=0.001)
check('import fields are 3-decimal clean',
      all(abs(round(ci[k]*1000)-ci[k]*1000) < 1e-6 for k in ('complex_scaled_area','complex_known_dim')), ci)
# single-tank page accepts the same fields and snaps to the same grid
code, j = post(one); ci = j['tanks'][0]['calculator_import']
form = {'config':'SC','layers':'2','shape_mode':'complex','complex_scaled_area':str(ci['complex_scaled_area']),
        'complex_known_dim':str(ci['complex_known_dim']),'complex_tank_perim':str(ci['complex_tank_perim']),
        'complex_excav_area':str(ci['complex_excav_area']),'complex_excav_perim':str(ci['complex_excav_perim']),
        'cover_stone':'1','base_stone':'0.333','perimeter_stone_width':'1','stone_void':'0.4','geoWaste':'20',
        'traffic_load':'HS20','void_enabled':'0'}
r = c.post('/', data=form)
html = re.sub(r'\s+', ' ', r.get_data(as_text=True))
check('single-tank page renders imported tank (200)', r.status_code==200)
check('single-tank snapped grid matches', f"({ci['crates_known']} × {ci['crates_other']} crates/layer)" in html, ci)
# 14. Review follow-up: import perimeter must reproduce the side-plate count exactly
def _sp(rows, cfg, lay):
    code, j = post({'tanks':[{'config':cfg,'layers':lay,'rows':rows}]}); t = j['tanks'][0]; ci = t['calculator_import']
    mt = A.calc_tank({'config':cfg,'layers':lay,'shape_mode':'complex','complex_scaled_area':ci['complex_scaled_area'],
                      'complex_known_dim':ci['complex_known_dim'],'complex_tank_perim':ci['complex_tank_perim'],
                      'complex_excav_area':ci['complex_excav_area'],'complex_excav_perim':ci['complex_excav_perim'],
                      'cover_stone':1.0,'base_stone':0.333,'perimeter_stone_width':1.0,'stone_void':0.4,'geoWaste':20})
    return t['bom']['side_plates']['qty'], mt['side_plates'], ci
a, b, ci = _sp([{'crate_count':2,'offset_crates':0}]*8, 'EX', 8)
check('EX 8-layer 8x2 side plates 165 == calc_tank (was 166 at 2dp perimeter)', a == 165 and b == 165, (a, b, ci['complex_tank_perim']))
random.seed(5); bad = []
for _ in range(400):
    rows = [{'crate_count':random.randint(1,12),'offset_crates':random.randint(0,6)} for _ in range(random.randint(1,12))]
    for cfg, lay in (('SC',random.randint(1,8)), ('EX',random.randint(1,8))):
        a, b, ci = _sp(rows, cfg, lay)
        if a != b: bad.append((cfg, lay, a, b))
check('side plates match calc_tank on 800 random tank/layer combos', not bad, bad[:3])
check('import dims are 3-decimal clean (perimeter + excavation)',
      all(abs(round(ci[k]*1000)-ci[k]*1000) < 1e-6 for k in ('complex_tank_perim','complex_excav_area','complex_excav_perim')), ci)
print('FAILS', fails)
sys.exit(1 if fails else 0)
