import bpy, math, json, numpy as np
from pathlib import Path
from mathutils import Vector
ROOT=Path('/Users/ultra/xp/3d-cli'); P=ROOT/'projects/low-poly-cat'; OUT=P/'reports/r11_search'; OUT.mkdir(parents=True,exist_ok=True)
scene=bpy.context.scene; scene.render.engine='BLENDER_WORKBENCH'; scene.display.shading.light='STUDIO'; scene.display.shading.color_type='MATERIAL'; scene.display.shading.show_shadows=False; scene.display.shading.show_cavity=False
views={'front':((0,.35,.085),(0,.015,.078),(133,256)),'threequarter':((.20,.32,.10),(0,.010,.080),(157,256)),'side':((.35,0,.09),(0,.010,.080),(177,256))}
refs={}
for v,(_,_,size) in views.items():
 im=bpy.data.images.load(str(P/'refs'/f'mask_{v}.png'),check_existing=False); a=np.array(im.pixels[:],dtype=np.float32).reshape(im.size[1],im.size[0],4); a=np.flipud(a); refs[v]=a[:,:,0]>.5
 bpy.data.images.remove(im)
objs={o.name:o for o in scene.objects if o.type=='MESH'}
base={n:np.array([tuple(v.co) for v in o.data.vertices],dtype=np.float64) for n,o in objs.items()}
centers={n:(a.min(0)+a.max(0))/2 for n,a in base.items()}

def reset():
 for n,o in objs.items():
  a=base[n]
  for i,v in enumerate(o.data.vertices): v.co=a[i]

def xf(names,scale=(1,1,1),shift=(0,0,0),anchor=None):
 for n in names:
  if n not in objs: continue
  a=base[n]; c=np.array(anchor if anchor is not None else centers[n]); q=(a-c)*np.array(scale)+c+np.array(shift)
  for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
head_parts=['Sculpted_Head_MASTER','Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER','Pink_Nose_MASTER','Recessed_Philtrum_MASTER','Mouth_Left_MASTER','Mouth_Right_MASTER']
haunch=['Haunch_Left_MASTER','Haunch_Right_MASTER']; fore=['Foreleg_Left_MASTER','Foreleg_Right_MASTER']; fpaw=['Front_paw_Left_MASTER','Front_paw_Right_MASTER']; hind=['Hind_paw_Left_MASTER','Hind_paw_Right_MASTER']

def apply(p):
 reset(); hc=np.array([0,.027,.115])
 for n in head_parts:
  if n in objs:
   a=base[n]; q=(a-hc)*np.array([p['head_x'],p['head_y'],1.0])+hc
   for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
 for n in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
  a=base[n]; c=centers[n]; s=1 if c[0]>0 else -1; q=(a-c)*np.array([p['ear_x'],p['ear_y'],1.0])+c; q[:,0]+=s*p['ear_sep'];
  for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
 for n in ['Rounded_Torso_MASTER']:
  a=base[n]; c=centers[n]; q=(a-c)*np.array([p['torso_x'],p['torso_y'],1.0])+c+np.array([0,p['torso_shift_y'],0]);
  for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
 for n in haunch:
  a=base[n]; c=centers[n]; s=1 if c[0]>0 else -1; q=(a-c)*np.array([p['haunch_x'],p['haunch_y'],1.0])+c; q[:,0]+=s*(p['haunch_sep']-1)*abs(c[0]); q[:,1]+=p['haunch_shift_y'];
  for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
 for n in fore+fpaw:
  a=base[n]; c=centers[n]; s=1 if c[0]>0 else -1; q=a.copy(); q[:,0]+=s*(p['fore_sep']-1)*abs(c[0]);
  for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
 n='Curled_tail_MASTER'; a=base[n]; q=a+np.array([p['tail_x'],p['tail_y'],0]);
 for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
def look(cam,target): cam.rotation_euler=(Vector(target)-cam.location).to_track_quat('-Z','Y').to_euler()
def render_mask(view):
 loc,target,size=views[view]; bpy.ops.object.camera_add(location=loc); cam=bpy.context.object; cam.data.type='ORTHO'; cam.data.ortho_scale=.18; look(cam,target); scene.camera=cam
 scene.render.resolution_x=size[0]; scene.render.resolution_y=size[1]; scene.render.resolution_percentage=100; tmp=OUT/f'_tmp_{view}.png'; scene.render.filepath=str(tmp); bpy.ops.render.render(write_still=True)
 im=bpy.data.images.load(str(tmp),check_existing=False); a=np.array(im.pixels[:],dtype=np.float32).reshape(im.size[1],im.size[0],4); a=np.flipud(a); bpy.data.images.remove(im); bpy.data.objects.remove(cam,do_unlink=True)
 return a[:,:,:3].max(axis=2)>.03

def reg_height(mask,ref):
 ys,xs=np.where(mask); ry,rx=np.where(ref); y0,y1=ys.min(),ys.max()+1; x0,x1=xs.min(),xs.max()+1; R0,R1=ry.min(),ry.max()+1; C0,C1=rx.min(),rx.max()+1
 crop=mask[y0:y1,x0:x1]; rh=R1-R0; scale=rh/crop.shape[0]; nw=max(1,round(crop.shape[1]*scale)); yi=np.minimum(crop.shape[0]-1,(np.arange(rh)/scale).astype(int)); xi=np.minimum(crop.shape[1]-1,(np.arange(nw)/scale).astype(int)); rs=crop[yi[:,None],xi[None,:]]
 placed=np.zeros_like(ref); cx=(C0+C1)//2; xx=round(cx-nw/2); xa=max(0,xx); xb=min(ref.shape[1],xx+nw); sa=xa-xx; sb=sa+(xb-xa); placed[R0:R0+rh,xa:xb]=rs[:,sa:sb]
 inter=np.logical_and(placed,ref).sum(); union=np.logical_or(placed,ref).sum(); return float(inter/union)
def score(p,save=False,label=''):
 apply(p); vals={v:reg_height(render_mask(v),refs[v]) for v in views}; vals['mean']=sum(vals.values())/3
 if save: (OUT/f'{label}.json').write_text(json.dumps({'params':p,'score':vals},indent=2))
 return vals
p={'head_x':1.0,'head_y':1.0,'ear_x':1.0,'ear_y':1.0,'ear_sep':0.0,'torso_x':1.0,'torso_y':1.0,'torso_shift_y':0.0,'haunch_x':1.0,'haunch_y':1.0,'haunch_sep':1.0,'haunch_shift_y':0.0,'fore_sep':1.0,'tail_x':0.0,'tail_y':0.0}
steps={'head_x':.035,'head_y':.05,'ear_x':.08,'ear_y':.12,'ear_sep':.0025,'torso_x':.035,'torso_y':.05,'torso_shift_y':.0035,'haunch_x':.05,'haunch_y':.05,'haunch_sep':.05,'haunch_shift_y':.0035,'fore_sep':.04,'tail_x':.004,'tail_y':.004}
bounds={'head_x':(.85,1.12),'head_y':(.82,1.18),'ear_x':(.75,1.35),'ear_y':(.7,1.5),'ear_sep':(-.007,.008),'torso_x':(.85,1.15),'torso_y':(.85,1.18),'torso_shift_y':(-.012,.012),'haunch_x':(.75,1.15),'haunch_y':(.8,1.2),'haunch_sep':(.75,1.12),'haunch_shift_y':(-.012,.012),'fore_sep':(.8,1.15),'tail_x':(-.012,.012),'tail_y':(-.012,.012)}
best=score(p,True,'initial'); print('INITIAL',best,p,flush=True)
for rnd in range(2):
 for k,st in steps.items():
  baseval=p[k]; winner=(best['mean'],baseval,best)
  for d in (-st,st):
   val=min(bounds[k][1],max(bounds[k][0],baseval+d)); cand=dict(p); cand[k]=val; sc=score(cand)
   print('TRY',rnd,k,val,sc,flush=True)
   if sc['mean']>winner[0]+1e-5: winner=(sc['mean'],val,sc)
  p[k]=winner[1]; best=winner[2]; print('ACCEPT',k,p[k],best,flush=True)
 for k in steps: steps[k]*=.55
score(p,True,'best'); apply(p); bpy.ops.wm.save_as_mainfile(filepath=str(P/'source/cat_fit_r11.blend'))
(P/'reports/fit_r11_params.json').write_text(json.dumps({'params':p,'score':best},indent=2)); print('BEST',best,p,flush=True)
