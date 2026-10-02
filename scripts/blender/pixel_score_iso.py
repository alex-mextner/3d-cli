#!/usr/bin/env python3
from PIL import Image
from pathlib import Path
import sys,json
ROOT=Path(__file__).resolve().parents[2]; P=ROOT/'projects/low-poly-cat'; run=sys.argv[1]
def render_mask(p):
 im=Image.open(p).convert('RGB'); out=Image.new('L',im.size,0); a=im.load(); b=out.load()
 for y in range(im.height):
  for x in range(im.width):
   if max(a[x,y])>12:b[x,y]=255
 return out
def iou_height(a,r):
 ab=a.getbbox(); rb=r.getbbox(); scale=(rb[3]-rb[1])/(ab[3]-ab[1]); nw=max(1,round((ab[2]-ab[0])*scale)); crop=a.crop(ab).resize((nw,rb[3]-rb[1]),Image.Resampling.NEAREST)
 p=Image.new('L',r.size,0); x=round((rb[0]+rb[2]-nw)/2); p.paste(crop,(x,rb[1])); A=p.load();R=r.load(); inter=union=0
 for y in range(r.height):
  for xx in range(r.width):
   av=A[xx,y]>0;rv=R[xx,y]>0;inter+=av and rv;union+=av or rv
 return inter/union
res={}
for v in ('front','threequarter','side'):
 a=render_mask(P/'renders'/run/f'{v}.png'); r=Image.open(P/'refs'/f'mask_{v}.png').convert('L');res[v]=iou_height(a,r)
res['mean']=sum(res.values())/3;(P/'reports'/f'{run}_pixel_iso.json').write_text(json.dumps(res,indent=2));print(json.dumps(res))