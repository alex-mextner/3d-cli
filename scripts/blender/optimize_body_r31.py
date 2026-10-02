import bpy,math,json,numpy as np
from pathlib import Path
from mathutils import Vector
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat'); OUT=P/'reports/r31_search'; OUT.mkdir(parents=True,exist_ok=True)
s=bpy.context.scene; s.render.engine='BLENDER_WORKBENCH'; s.display.shading.light='STUDIO'; s.display.shading.color_type='MATERIAL'; s.display.shading.show_shadows=False; s.display.shading.show_cavity=False
sizes={'front':(133,256),'threequarter':(157,256),'side':(177,256)}; refs={}
for name in sizes:
 im=bpy.data.images.load(str(P/'refs'/f'mask_{name}.png'),check_existing=False); a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); refs[name]=np.flipud(a)[:,:,0]>.5; bpy.data.images.remove(im)
objs={o.name:o for o in s.objects if o.type=='MESH'}; base={n:np.array([tuple(v.co) for v in o.data.vertices],float) for n,o in objs.items()}; cen={n:(q.min(0)+q.max(0))/2 for n,q in base.items()}
def put(n,q):
 for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
def reset():
 for n,q in base.items(): put(n,q)
def aff(n,sx=1,sy=1,dx=0,dy=0):
 q=base[n].copy(); c=cen[n]; q[:,0]=c[0]+(q[:,0]-c[0])*sx+dx; q[:,1]=c[1]+(q[:,1]-c[1])*sy+dy; put(n,q)
def apply(p):
 reset(); n='Rounded_Torso_MASTER'; q=base[n].copy(); c=cen[n]; q[:,0]=c[0]+(q[:,0]-c[0])*p['torso_sx']+p['torso_dx']; q[:,1]=c[1]+(q[:,1]-c[1])*p['torso_sy']+p['torso_dy']; low=np.clip((.075-q[:,2])/.045,0,1); neg=q[:,0]<c[0]; pos=~neg; q[neg,0]=c[0]+(q[neg,0]-c[0])*(1-(1-p['torso_neg'])*low[neg]); q[pos,0]=c[0]+(q[pos,0]-c[0])*(1-(1-p['torso_pos'])*low[pos]); put(n,q)
 for n,k in [('Haunch_Left_MASTER','L'),('Haunch_Right_MASTER','R')]: aff(n,p[f'h{k}_sx'],p[f'h{k}_sy'],p[f'h{k}_dx'],p[f'h{k}_dy'])
 aff('Curled_tail_MASTER',1,1,p['tail_dx'],p['tail_dy'])
 for n,k in [('Foreleg_Left_MASTER','L'),('Foreleg_Right_MASTER','R')]: aff(n,1,1,p[f'f{k}_dx'],0)
 for n,k in [('Front_paw_Left_MASTER','L'),('Front_paw_Right_MASTER','R')]: aff(n,1,1,p[f'f{k}_dx'],0)
def look(c,t): c.rotation_euler=(Vector(t)-c.location).to_track_quat('-Z','Y').to_euler()
def render(v):
 if v=='front': loc=(0,.35,.085); tgt=(0,.015,.078)
 elif v=='side': loc=(.35,0,.09); tgt=(0,.010,.080)
 else:
  r=(.20**2+.32**2)**.5; a=math.radians(28.25); loc=(r*math.sin(a),r*math.cos(a),.10); tgt=(0,.010,.080)
 bpy.ops.object.camera_add(location=loc); c=bpy.context.object; c.data.type='ORTHO'; c.data.ortho_scale=.18; look(c,tgt); s.camera=c; s.render.resolution_x=sizes[v][0];s.render.resolution_y=sizes[v][1];s.render.resolution_percentage=100; tmp=OUT/f'_tmp_{v}.png'; s.render.filepath=str(tmp); bpy.ops.render.render(write_still=True); bpy.data.objects.remove(c,do_unlink=True)
 im=bpy.data.images.load(str(tmp),check_existing=False); a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); a=np.flipud(a); bpy.data.images.remove(im); return a[:,:,:3].max(2)>.03

def reg(mask,ref):
 y,x=np.where(mask); ry,rx=np.where(ref); y0,y1=y.min(),y.max()+1; x0,x1=x.min(),x.max()+1; R0,R1=ry.min(),ry.max()+1; C0,C1=rx.min(),rx.max()+1; crop=mask[y0:y1,x0:x1]; sc=(R1-R0)/crop.shape[0]; nw=max(1,round(crop.shape[1]*sc)); yi=np.minimum(crop.shape[0]-1,(np.arange(R1-R0)/sc).astype(int)); xi=np.minimum(crop.shape[1]-1,(np.arange(nw)/sc).astype(int)); rs=crop[yi[:,None],xi[None,:]]; pl=np.zeros_like(ref); xx=round((C0+C1-nw)/2); xa=max(0,xx); xb=min(ref.shape[1],xx+nw); pl[R0:R1,xa:xb]=rs[:,xa-xx:xa-xx+xb-xa]; u=np.logical_or(pl,ref).sum(); return float(np.logical_and(pl,ref).sum()/u)
def score(p):
 apply(p); d={v:reg(render(v),refs[v]) for v in sizes}; d['mean']=sum(d.values())/3; d['objective']=.34*d['front']+.41*d['threequarter']+.25*d['side']-2.0*sum(max(0,.81-d[v])**2 for v in sizes); return d
p={'torso_sx':1.,'torso_sy':1.,'torso_dx':0.,'torso_dy':0.,'torso_neg':1.,'torso_pos':1.,'hL_sx':1.,'hL_sy':1.,'hL_dx':0.,'hL_dy':0.,'hR_sx':1.,'hR_sy':1.,'hR_dx':0.,'hR_dy':0.,'tail_dx':0.,'tail_dy':0.,'fL_dx':0.,'fR_dx':0.}
steps={'torso_sx':.04,'torso_sy':.05,'torso_dx':.0015,'torso_dy':.0025,'torso_neg':.05,'torso_pos':.05,'hL_sx':.06,'hL_sy':.06,'hL_dx':.0015,'hL_dy':.002,'hR_sx':.06,'hR_sy':.06,'hR_dx':.0015,'hR_dy':.002,'tail_dx':.002,'tail_dy':.002,'fL_dx':.0012,'fR_dx':.0012}
bounds={k:(.75,1.25) for k in ('torso_sx','torso_sy','torso_neg','torso_pos','hL_sx','hL_sy','hR_sx','hR_sy')}; bounds.update({k:(-.008,.008) for k in ('torso_dx','torso_dy','hL_dx','hL_dy','hR_dx','hR_dy','tail_dx','tail_dy','fL_dx','fR_dx')})
best=score(p); print('INIT',best,p,flush=True)
for rnd in range(3):
 for k,st in list(steps.items()):
  bv=p[k]; win=(best['objective'],bv,best)
  for dlt in (-st,st):
   val=max(bounds[k][0],min(bounds[k][1],bv+dlt)); c=dict(p); c[k]=val; sc=score(c); print('TRY',rnd,k,val,sc,flush=True)
   if sc['objective']>win[0]+1e-5: win=(sc['objective'],val,sc)
  p[k]=win[1]; best=win[2]; print('ACCEPT',k,p[k],best,flush=True)
 for k in steps: steps[k]*=.5
apply(p); out=P/'source/cat_fit_r31.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); (P/'reports/fit_r31_params.json').write_text(json.dumps({'params':p,'score':best},indent=2)); print('BEST',best,p,flush=True)