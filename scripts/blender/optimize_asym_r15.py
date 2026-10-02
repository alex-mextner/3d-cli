import bpy, math, json, numpy as np
from pathlib import Path
from mathutils import Vector
ROOT=Path('/Users/ultra/xp/3d-cli'); P=ROOT/'projects/low-poly-cat'; OUT=P/'reports/r15_search'; OUT.mkdir(parents=True,exist_ok=True)
s=bpy.context.scene; s.render.engine='BLENDER_WORKBENCH'; s.display.shading.light='STUDIO'; s.display.shading.color_type='MATERIAL'; s.display.shading.show_shadows=False; s.display.shading.show_cavity=False
refs={}
sizes={'front':(133,256),'threequarter':(157,256),'side':(177,256)}
for v,size in sizes.items():
 im=bpy.data.images.load(str(P/'refs'/f'mask_{v}.png'),check_existing=False); q=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); refs[v]=np.flipud(q)[:,:,0]>.5; bpy.data.images.remove(im)
objs={o.name:o for o in s.objects if o.type=='MESH'}; base={n:np.array([tuple(v.co) for v in o.data.vertices],float) for n,o in objs.items()}; cen={n:(a.min(0)+a.max(0))/2 for n,a in base.items()}
def put(n,q):
 for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
def reset():
 for n,q in base.items(): put(n,q)
def apply(p):
 reset()
 for n in ('Sculpted_Head_MASTER','Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER','Pink_Nose_MASTER','Recessed_Philtrum_MASTER','Mouth_Left_MASTER','Mouth_Right_MASTER','Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
  if n in objs: q=base[n].copy(); q[:,0]+=p['head_dx']; put(n,q)
 o='Rounded_Torso_MASTER'; q=base[o].copy(); low=np.clip((.075-q[:,2])/.045,0,1); neg=q[:,0]<0; pos=~neg; q[neg,0]*=(1-(1-p['torso_neg'])*low[neg]); q[pos,0]*=(1-(1-p['torso_pos'])*low[pos]); put(o,q)
 for n,key in (('Haunch_Left_MASTER','L'),('Haunch_Right_MASTER','R')):
  q=base[n].copy(); c=cen[n]; q[:,0]=(q[:,0]-c[0])*p[f'h{key}_sx']+c[0]+p[f'h{key}_dx']; put(n,q)
 q=base['Curled_tail_MASTER'].copy(); q[:,0]+=p['tail_dx']; put('Curled_tail_MASTER',q)
 for n in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
  q=np.array([tuple(v.co) for v in objs[n].data.vertices]); sg=1 if q[:,0].mean()>0 else -1; q[:,0]+=sg*p['ear_sep']; put(n,q)
def look(c,t): c.rotation_euler=(Vector(t)-c.location).to_track_quat('-Z','Y').to_euler()
def render_mask(v,p):
 if v=='front': loc=(0,.35,.085); target=(0,.015,.078)
 elif v=='side': loc=(.35,0,.09); target=(0,.010,.080)
 else:
  r=(.20**2+.32**2)**.5; a=math.radians(p['tq_angle']); loc=(r*math.sin(a),r*math.cos(a),.10); target=(0,.010,.080)
 bpy.ops.object.camera_add(location=loc); c=bpy.context.object; c.data.type='ORTHO'; c.data.ortho_scale=.18; look(c,target); s.camera=c; s.render.resolution_x=sizes[v][0]; s.render.resolution_y=sizes[v][1]; s.render.resolution_percentage=100; tmp=OUT/f'_tmp_{v}.png'; s.render.filepath=str(tmp); bpy.ops.render.render(write_still=True)
 im=bpy.data.images.load(str(tmp),check_existing=False); a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); a=np.flipud(a); bpy.data.images.remove(im); bpy.data.objects.remove(c,do_unlink=True); return a[:,:,:3].max(2)>.03
def reg(mask,ref):
 y,x=np.where(mask); ry,rx=np.where(ref); y0,y1=y.min(),y.max()+1; x0,x1=x.min(),x.max()+1; R0,R1=ry.min(),ry.max()+1; C0,C1=rx.min(),rx.max()+1; crop=mask[y0:y1,x0:x1]; sc=(R1-R0)/crop.shape[0]; nw=max(1,round(crop.shape[1]*sc)); yi=np.minimum(crop.shape[0]-1,(np.arange(R1-R0)/sc).astype(int)); xi=np.minimum(crop.shape[1]-1,(np.arange(nw)/sc).astype(int)); rs=crop[yi[:,None],xi[None,:]]; placed=np.zeros_like(ref); xx=round((C0+C1-nw)/2); xa=max(0,xx); xb=min(ref.shape[1],xx+nw); placed[R0:R1,xa:xb]=rs[:,xa-xx:xa-xx+xb-xa]; return float(np.logical_and(placed,ref).sum()/np.logical_or(placed,ref).sum())
def score(p):
 apply(p); d={v:reg(render_mask(v,p),refs[v]) for v in sizes}; d['weighted']=.48*d['front']+.27*d['threequarter']+.25*d['side']; d['mean']=(d['front']+d['threequarter']+d['side'])/3; return d
p={'head_dx':0.0,'torso_neg':1.0,'torso_pos':1.0,'hL_sx':1.0,'hR_sx':1.0,'hL_dx':0.0,'hR_dx':0.0,'tail_dx':0.0,'ear_sep':0.0,'tq_angle':32.0}
steps={'head_dx':.0012,'torso_neg':.08,'torso_pos':.04,'hL_sx':.07,'hR_sx':.10,'hL_dx':.0015,'hR_dx':.002,'tail_dx':.0025,'ear_sep':.0015,'tq_angle':3.0}
bounds={'head_dx':(-.005,.003),'torso_neg':(.55,1.08),'torso_pos':(.75,1.08),'hL_sx':(.65,1.15),'hR_sx':(.5,1.1),'hL_dx':(-.006,.008),'hR_dx':(-.004,.010),'tail_dx':(-.006,.014),'ear_sep':(-.006,.008),'tq_angle':(20,40)}
best=score(p); print('INIT',best,p,flush=True)
for rnd in range(2):
 for k,st in list(steps.items()):
  bv=p[k]; win=(best['weighted'],bv,best)
  for d in (-st,st):
   val=max(bounds[k][0],min(bounds[k][1],bv+d)); c=dict(p); c[k]=val; sc=score(c); print('TRY',rnd,k,val,sc,flush=True)
   if sc['weighted']>win[0]+1e-5: win=(sc['weighted'],val,sc)
  p[k]=win[1]; best=win[2]; print('ACCEPT',k,p[k],best,flush=True)
 for k in steps: steps[k]*=.5
apply(p); bpy.ops.wm.save_as_mainfile(filepath=str(P/'source/cat_fit_r15.blend')); (P/'reports/fit_r15_params.json').write_text(json.dumps({'params':p,'score':best},indent=2)); print('BEST',best,p,flush=True)
