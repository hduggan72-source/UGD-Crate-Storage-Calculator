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
    check(tag+' contingency', t['bom']['contingency']['qty']==ct['contingency'])
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
check('per-tank contingency 46 each', all(t['bom']['contingency']['qty']==46 for t in j['tanks']))
check('project contingency 36 (not 92)', j['totals']['bom']['contingency']['qty']==36, j['totals']['bom']['contingency'])
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
print('FAILS', fails)
sys.exit(1 if fails else 0)
