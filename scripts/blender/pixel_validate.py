#!/usr/bin/env python3
from PIL import Image
from pathlib import Path
import json, sys, math
ROOT=Path(__file__).resolve().parents[2]; PROJ=ROOT/'projects/low-poly-cat'
profiles=json.loads((PROJ/'refs/reference_profiles.json').read_text())
run=sys.argv[1] if len(sys.argv)>1 else 'fit_r9'
outdir=PROJ/'reports'/f'{run}_pixel'; outdir.mkdir(parents=True,exist_ok=True)

def mask_render(path):
 im=Image.open(path).convert('RGB'); px=im.load(); m=Image.new('L',im.size,0); q=m.load()
 for y in range(im.height):
  for x in range(im.width):
   r,g,b=px[x,y]
   if max(r,g,b)>12: q[x,y]=255
 return m

def bbox(mask):
 box=mask.getbbox();
 if not box: raise RuntimeError('empty mask')
 return box

def profile(mask,box):
 x0,y0,x1,y1=box; rows=[]
 for t in [i/20 for i in range(21)]:
  y=min(y1-1,round(y0+t*(y1-y0-1))); xs=[x for x in range(mask.width) if mask.getpixel((x,y))>0]
  rows.append((t,min(xs) if xs else None,max(xs)+1 if xs else None))
 return rows
def compare_profile(actual,abox,ref):
 x0,y0,x1,y1=abox; aw=x1-x0
 errs=[]
 for (_,ax0,ax1),(_,rx0,rx1) in zip(actual,ref['width_profile']):
  if ax0 is None or rx0 is None: continue
  ac=((ax0+ax1)/2-x0)/aw; awid=(ax1-ax0)/aw
  rb=ref['bbox']; rw=rb[2]-rb[0]
  rc=((rx0+rx1)/2-rb[0])/rw; rwid=(rx1-rx0)/rw
  errs.append((abs(ac-rc),abs(awid-rwid)))
 return {'center_mae':sum(e[0] for e in errs)/len(errs),'width_mae':sum(e[1] for e in errs)/len(errs)}

def registered_iou(actual, refmask):
 ab=bbox(actual); rb=bbox(refmask)
 acrop=actual.crop(ab).resize((rb[2]-rb[0],rb[3]-rb[1]),Image.Resampling.NEAREST)
 placed=Image.new('L',refmask.size,0); placed.paste(acrop,(rb[0],rb[1]))
 a=placed.load(); r=refmask.load(); inter=union=0
 ov=Image.new('RGB',refmask.size,'black'); op=ov.load()
 for y in range(refmask.height):
  for x in range(refmask.width):
   av=a[x,y]>0; rv=r[x,y]>0; inter+=av and rv; union+=av or rv
   if av and rv: op[x,y]=(255,255,255)
   elif rv: op[x,y]=(255,0,0)
   elif av: op[x,y]=(0,255,255)
 return inter/union,ov
report={'run':run,'views':{}}
for view in ('front','threequarter','side'):
 path=PROJ/'renders'/run/f'{view}.png'; m=mask_render(path); b=bbox(m); p=profile(m,b); ref=profiles[view]
 metric=compare_profile(p,b,ref); metric['render_bbox']=list(b); metric['render_size']=list(m.size)
 metric['bbox_aspect']= (b[2]-b[0])/(b[3]-b[1]); rb=ref['bbox']; metric['reference_bbox_aspect']=(rb[2]-rb[0])/(rb[3]-rb[1])
 rm=Image.open(PROJ/'refs'/f'mask_{view}.png').convert('L'); iou,ov=registered_iou(m,rm); metric['registered_pixel_iou']=iou; ov.save(outdir/f'{view}_overlay.png')
 report['views'][view]=metric
score=sum(v['center_mae']+v['width_mae'] for v in report['views'].values())/6
report['profile_error_mean']=score
(PROJ/'reports'/f'{run}_pixel.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))

# Isotropic height registration: camera scale may match height, but geometry width is preserved.
def height_iou(actual, refmask):
 ab=bbox(actual); rb=bbox(refmask); ah=ab[3]-ab[1]; rh=rb[3]-rb[1]
 scale=rh/ah; nw=max(1,round((ab[2]-ab[0])*scale)); crop=actual.crop(ab).resize((nw,rh),Image.Resampling.NEAREST)
 placed=Image.new('L',refmask.size,0); rx=(rb[0]+rb[2])//2; x=round(rx-nw/2); placed.paste(crop,(x,rb[1]))
 a=placed.load(); r=refmask.load(); inter=union=0
 for y in range(refmask.height):
  for xx in range(refmask.width):
   av=a[xx,y]>0; rv=r[xx,y]>0; inter+=av and rv; union+=av or rv
 return inter/union
