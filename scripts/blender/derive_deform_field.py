#!/usr/bin/env python3
from PIL import Image
from pathlib import Path
import numpy as np,json,sys
ROOT=Path(__file__).resolve().parents[2]; P=ROOT/'projects/low-poly-cat'; run=sys.argv[1]; alpha=float(sys.argv[2]) if len(sys.argv)>2 else .45

def mrender(p): return np.asarray(Image.open(p).convert('RGB')).max(2)>12
def mref(p): return np.asarray(Image.open(p).convert('L'))>127
def bbox(m):
 y,x=np.where(m); return (x.min(),y.min(),x.max()+1,y.max()+1)
def sample(m,b,ts):
 x0,y0,x1,y1=b; h=y1-y0; out=[]
 for t in ts:
  y=min(y1-1,max(y0,round(y0+t*(h-1)))); x=np.where(m[y])[0]
  if not len(x): out.append((np.nan,np.nan)); continue
  out.append(((x[-1]-x[0]+1)/h,(((x[0]+x[-1]+1)/2)-((x0+x1)/2))/h))
 return np.array(out,float)
ts=np.linspace(.03,.97,48); field={'run':run,'alpha':alpha,'t':ts.tolist()}
for axis,view in [('x','front'),('y','side')]:
 cm=mrender(P/'renders'/run/f'{view}.png'); rm=mref(P/'refs'/f'mask_{view}.png'); cb=bbox(cm); rb=bbox(rm); c=sample(cm,cb,ts); r=sample(rm,rb,ts)
 ratio=np.divide(r[:,0],c[:,0],out=np.ones(len(ts)),where=np.isfinite(c[:,0])&(c[:,0]>1e-6)); shift=r[:,1]-c[:,1]
 ratio=np.clip(1+alpha*(ratio-1),.88,1.12); shift=np.clip(alpha*shift,-.025,.025)
 # smooth 5-tap triangular kernel
 k=np.array([1,2,3,2,1],float);k/=k.sum();ratio=np.convolve(np.pad(ratio,(2,2),mode='edge'),k,mode='valid');shift=np.convolve(np.pad(shift,(2,2),mode='edge'),k,mode='valid')
 field[axis]={'scale':ratio.tolist(),'shiftH':shift.tolist(),'current_centerH':c[:,1].tolist(),'current_widthH':c[:,0].tolist(),'target_widthH':r[:,0].tolist()}
out=P/'reports'/f'{run}_deform_field.json';out.write_text(json.dumps(field,indent=2));print(out)