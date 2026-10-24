"""Read-only exploration of local moderated JPEGs; outputs stay under ignored data/."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowapp.config import load_config
from flowapp.quality import inspect_image


def region_metrics(path, tile_size=256, min_contrast=10):
    with Image.open(path) as im:
        a=np.asarray(im.convert('L'),dtype=np.float32)
    values=[]
    for y in range(0,a.shape[0]-tile_size+1,tile_size):
        for x in range(0,a.shape[1]-tile_size+1,tile_size):
            t=a[y:y+tile_size,x:x+tile_size]; c=t[1:-1,1:-1]
            lap=t[:-2,1:-1]+t[2:,1:-1]+t[1:-1,:-2]+t[1:-1,2:]-4*c
            gx=t[1:-1,2:]-t[1:-1,:-2];gy=t[2:,1:-1]-t[:-2,1:-1]
            gradient=float(np.mean(gx*gx+gy*gy))
            if t.std()<min_contrast:continue
            values.append((float(lap.var()),gradient,float(lap.var())/max(gradient,1e-6)))
    v=np.array(values)
    out={}
    for i,name in enumerate(['local_sharpness','gradient','lap_gradient_ratio']):
        for q in [10,25,50,75,90]:out[f'{name}_p{q}']=float(np.percentile(v[:,i],q)) if len(v) else 0
    # JPEG 8x8 boundary excess compared with adjacent differences.
    dx=np.abs(np.diff(a,axis=1));dy=np.abs(np.diff(a,axis=0))
    for d,name in [(dx,'x'),(dy.T,'y')]:
        boundary=float(d[:,7::8].mean()); inside=float(np.delete(d,np.arange(7,d.shape[1],8),axis=1).mean())
        out['block_'+name]=boundary/max(inside,1e-6)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tile-size', type=int, default=256)
    parser.add_argument('--min-contrast', type=float, default=10)
    parser.add_argument('--validate', action='store_true', help='Avaliar os limiares atuais e salvar o relatório local')
    args = parser.parse_args()
    config = load_config(Path('config.json'))
    if args.tile_size < 3 or args.min_contrast < 0:
        parser.error('tile-size deve ser >= 3 e min-contrast >= 0')
    db=sqlite3.connect('file:data/flow.sqlite3?mode=ro',uri=True)
    rows=[]
    for status,title,reason,cid in db.execute("select status,title,reason,concept_id from moderation_records where concept_id is not null and (status='approved' or reason like '%calidad%')"):
        assets=db.execute("select path,export_json from assets where concept_id=? and role='upscaled' order by rowid desc",(cid,)).fetchall()
        if not assets:continue
        path,export=assets[0];path=Path(json.loads(export).get('path',path))
        if path.exists():
            if args.validate:
                rows.append(dict(title=title, moderation=status,
                                 **inspect_image(path, config.quality_rules, config.export_rules)))
            else:
                rows.append(dict(title=title,status=status,**region_metrics(path, args.tile_size, args.min_contrast)))
    db.close()
    output = Path('data/quality-calibration-validation.json') if args.validate else Path(f'data/quality-regions-{args.tile_size}.json')
    output.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.validate:
        for status, expected in [('approved', 'approved'), ('rejected', 'warning')]:
            group = [r for r in rows if r['moderation'] == status]
            passed = sum(r['status'] == expected for r in group)
            print(f'{status}: {passed}/{len(group)} com resultado {expected}')
        return
    for row in rows: print(row['title'],row['status'],json.dumps({k:round(v,2) for k,v in row.items() if k not in ['title','status']}))
if __name__=='__main__':main()
