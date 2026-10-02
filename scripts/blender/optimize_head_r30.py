import bpy,math,json,numpy as np
from pathlib import Path
from mathutils import Vector
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat'); OUT=P/'reports/r30_search'; OUT.mkdir(parents=True,exist_ok=True)
s=bpy.context.scene; s.render.engine='BLENDER_WORKBENCH'; s.display.shading.light='STUDIO'; s.display.shading.color_type='MATERIAL'; s.display.shading.show_shadows=False; s.display.shading.show_cavity=False
sizes={'front':(133,256),'threequarter':(157,256),'side':(177,256)}; refs={}
for name in sizes:
 im=bpy.data.images.load(str(P/'refs'/f'mask_{name}.png'),check_existing=False); a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); refs[name]=np.flipud(a)[:,:,0]>.5; bpy.data.images.remove(im)
objs={o.name:o for o in s.objects if o.type=='MESH'}; base={n:np.array([tuple(v.co) for v in o.data.vertices],float) for n,o in objs.items()}; cen={n:(q.min(0)+q.max(0))/2 for n,q in base.items()}
head_parts=[n for n in objs if n.startswith(('Sculpted_Head','Inset_Eye','Pink_Nose','Recessed_Philtrum','Mouth_'))]
ears=[n for n in objs if n.startswith('Thick_Ear_')]
def put(n,q):
 for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
def reset():
 for n,q in base.items(): put(n,q)
def apply(p):
 reset(); hc=cen['Sculpted_Head_MASTER'].copy()
 for n in head_parts:
  q=base[n].copy(); q[:,0]=hc[0]+(q[:,0]-hc[0])*p['head_sx']; q[:,1]=hc[1]+(q[:,1]-hc[1])*p['head_sy']; q[:,2]=hc[2]+(q[:,2]-hc[2])*p['head_sz']+p['head_dz']; put(n,q)
 for n in ears:
  q=base[n].copy(); c=cen[n]; sg=1 if c[0]>0 else -1; q[:,0]=c[0]+(q[:,0]-c[0])*p['ear_sx']+sg*p['ear_sep']; q[:,1]=c[1]+(q[:,1]-c[1])*p['ear_sy']; q[:,2]=c[2]+(q[:,2]-c[2])*p['ear_sz']+p['ear_dz']; put(n,q)
def look(c,t): c.rotation_euler=(Vector(t)-c.location).to_track_quat('-Z','Y').to_euler()
def render(v):
 if v=='front': loc=(0,.35,.085); tgt=(0,.015,.078)
 elif v=='side': loc=(.35,0,.09); tgt=(0,.010,.080)
 else:
  r=(.20**2+.32**2)**.5; a=math.radians(28.25); loc=(r*math.sin(a),r*math.cos(a),.10); tgt=(0,.010,.080)
 bpy.ops.object.camera_add(location=loc); c=bpy.context.object; c.data.type='ORTHO'; c.data.ortho_scale=.18; look(c,tgt); s.camera=c; s.render.resolution_x=sizes[v][0];s.render.resolution_y=sizes[v][1];s.render.resolution_percentage=100; tmp=OUT/f'_tmp_{v}.png';s.render.filepath=str(tmp);bpy.ops.render.render(write_still=True);bpy.data.objects.remove(c,do_unlink=True)
 im=bpy.data.images.load(str(tmp),check_existing=False); a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); a=np.flipud(a); bpy.data.images.remove(im); return a[:,:,:3].max(2)>.03

def registered(mask,ref):
 y,x=np.where(mask); ry,rx=np.where(ref); y0,y1=y.min(),y.max()+1; x0,x1=x.min(),x.max()+1; R0,R1=ry.min(),ry.max()+1; C0,C1=rx.min(),rx.max()+1
 crop=mask[y0:y1,x0:x1]; sc=(R1-R0)/crop.shape[0]; nw=max(1,round(crop.shape[1]*sc)); yi=np.minimum(crop.shape[0]-1,(np.arange(R1-R0)/sc).astype(int)); xi=np.minimum(crop.shape[1]-1,(np.arange(nw)/sc).astype(int)); rs=crop[yi[:,None],xi[None,:]]
 placed=np.zeros_like(ref); xx=round((C0+C1-nw)/2); xa=max(0,xx); xb=min(ref.shape[1],xx+nw); placed[R0:R1,xa:xb]=rs[:,xa-xx:xa-xx+xb-xa]
 return placed,(R0,R1,C0,C1)
def iou(a,b):
 u=np.logical_or(a,b).sum(); return float(np.logical_and(a,b).sum()/u) if u else 1.0
def score(p):
 apply(p); out={}; hs={}
 for v in sizes:
  pl,b=registered(render(v),refs[v]); out[v]=iou(pl,refs[v]); R0,R1,C0,C1=b; cut=R0+round((R1-R0)*.34); hs[v]=iou(pl[R0:cut],refs[v][R0:cut])
 out['mean']=sum(out[v] for v in sizes)/3; out['head_mean']=sum(hs.values())/3; out['objective']=.72*out['mean']+.28*out['head_mean']; out['head']=hs; return out
p={'head_sx':1.0,'head_sy':1.0,'head_sz':1.0,'head_dz':0.0,'ear_sx':1.0,'ear_sy':1.0,'ear_sz':1.0,'ear_sep':0.0,'ear_dz':0.0}
steps={'head_sx':.035,'head_sy':.05,'head_sz':.035,'head_dz':.002,'ear_sx':.06,'ear_sy':.08,'ear_sz':.04,'ear_sep':.0015,'ear_dz':.0015}
bounds={'head_sx':(.9,1.1),'head_sy':(.85,1.15),'head_sz':(.9,1.1),'head_dz':(-.006,.006),'ear_sx':(.8,1.2),'ear_sy':(.75,1.3),'ear_sz':(.85,1.15),'ear_sep':(-.006,.006),'ear_dz':(-.005,.005)}
best=score(p); print('INIT',best,p,flush=True)
for rnd in range(3):
 for k,st in list(steps.items()):
  bv=p[k]; win=(best['objective'],bv,best)
  for d in (-st,st):
   val=max(bounds[k][0],min(bounds[k][1],bv+d)); c=dict(p); c[k]=val; sc=score(c); print('TRY',rnd,k,val,sc,flush=True)
   if sc['objective']>win[0]+1e-5: win=(sc['objective'],val,sc)
  p[k]=win[1]; best=win[2]; print('ACCEPT',k,p[k],best,flush=True)
 for k in steps: steps[k]*=.55
apply(p); out=P/'source/cat_fit_r30.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); (P/'reports/fit_r30_params.json').write_text(json.dumps({'params':p,'score':best},indent=2)); print('BEST',best,p,flush=True)