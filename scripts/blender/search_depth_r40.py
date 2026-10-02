import bpy,math,json,numpy as np
from pathlib import Path
from mathutils import Vector
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat');OUT=P/'reports/r40_search';OUT.mkdir(parents=True,exist_ok=True)
s=bpy.context.scene;s.render.engine='BLENDER_WORKBENCH';s.display.shading.light='STUDIO';s.display.shading.color_type='MATERIAL';s.display.shading.show_shadows=False;s.display.shading.show_cavity=False
sizes={'threequarter':(157,256),'side':(177,256)};refs={}
for n in sizes:
 im=bpy.data.images.load(str(P/'refs'/f'mask_{n}.png'),check_existing=False);a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4);refs[n]=np.flipud(a)[:,:,0]>.5;bpy.data.images.remove(im)
objs=[o for o in s.objects if o.type=='MESH' and not o.name.startswith(('REF_','QA_'))];base={o.name:np.array([tuple(v.co) for v in o.data.vertices],float) for o in objs}; cy=np.mean(np.vstack(list(base.values()))[:,1])
def apply(sy):
 for o in objs:
  q=base[o.name].copy();q[:,1]=cy+(q[:,1]-cy)*sy
  for i,v in enumerate(o.data.vertices):v.co=q[i]
def look(c,t):c.rotation_euler=(Vector(t)-c.location).to_track_quat('-Z','Y').to_euler()
def render(v,ang):
 if v=='side':loc=(.35,0,.09);tgt=(0,.010,.080)
 else:
  r=(.20**2+.32**2)**.5;a=math.radians(ang);loc=(r*math.sin(a),r*math.cos(a),.10);tgt=(0,.010,.080)
 bpy.ops.object.camera_add(location=loc);c=bpy.context.object;c.data.type='ORTHO';c.data.ortho_scale=.18;look(c,tgt);s.camera=c;s.render.resolution_x=sizes[v][0];s.render.resolution_y=sizes[v][1];s.render.resolution_percentage=100;tmp=OUT/f'_{v}.png';s.render.filepath=str(tmp);bpy.ops.render.render(write_still=True);bpy.data.objects.remove(c,do_unlink=True);im=bpy.data.images.load(str(tmp),check_existing=False);a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4);a=np.flipud(a);bpy.data.images.remove(im);return a[:,:,:3].max(2)>.03

def reg(mask,ref):
 y,x=np.where(mask);ry,rx=np.where(ref);y0,y1=y.min(),y.max()+1;x0,x1=x.min(),x.max()+1;R0,R1=ry.min(),ry.max()+1;C0,C1=rx.min(),rx.max()+1;crop=mask[y0:y1,x0:x1];sc=(R1-R0)/crop.shape[0];nw=max(1,round(crop.shape[1]*sc));yi=np.minimum(crop.shape[0]-1,(np.arange(R1-R0)/sc).astype(int));xi=np.minimum(crop.shape[1]-1,(np.arange(nw)/sc).astype(int));rs=crop[yi[:,None],xi[None,:]];pl=np.zeros_like(ref);xx=round((C0+C1-nw)/2);xa=max(0,xx);xb=min(ref.shape[1],xx+nw);pl[R0:R1,xa:xb]=rs[:,xa-xx:xa-xx+xb-xa];return float(np.logical_and(pl,ref).sum()/np.logical_or(pl,ref).sum())
best=(-1,None,None,None)
for sy in np.linspace(.93,1.01,9):
 apply(float(sy)); side=reg(render('side',28.25),refs['side'])
 for ang in np.arange(26.0,34.1,1.0):
  tq=reg(render('threequarter',float(ang)),refs['threequarter']);obj=.52*tq+.48*side
  print('TRY',sy,ang,'tq',tq,'side',side,'obj',obj,flush=True)
  if obj>best[0]:best=(obj,float(sy),float(ang),{'threequarter':tq,'side':side})
print('BEST',best,flush=True);apply(best[1]);bpy.ops.wm.save_as_mainfile(filepath=str(P/'source/cat_fit_r40.blend'));(P/'reports/fit_r40_params.json').write_text(json.dumps({'params':{'tq_angle':best[2],'depth_scale':best[1]},'score':best[3]},indent=2))