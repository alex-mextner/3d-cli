import bpy,math,json,numpy as np
from pathlib import Path
from mathutils import Vector
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat'); OUT=P/'reports/r32_search'; OUT.mkdir(parents=True,exist_ok=True)
s=bpy.context.scene; s.render.engine='BLENDER_WORKBENCH'; s.display.shading.light='STUDIO'; s.display.shading.color_type='MATERIAL'; s.display.shading.show_shadows=False; s.display.shading.show_cavity=False
sizes={'front':(133,256),'threequarter':(157,256),'side':(177,256)}; refs={}
for n in sizes:
 im=bpy.data.images.load(str(P/'refs'/f'mask_{n}.png'),check_existing=False); a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); refs[n]=np.flipud(a)[:,:,0]>.5; bpy.data.images.remove(im)
objs={o.name:o for o in s.objects if o.type=='MESH'}; base={n:np.array([tuple(v.co) for v in o.data.vertices],float) for n,o in objs.items()}; parts=[n for n in objs if n.startswith(('Sculpted_Head','Inset_Eye','Pink_Nose','Recessed_Philtrum','Mouth_','Thick_Ear_'))]
def reset():
 for n,q in base.items():
  for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
def apply(dx,dy):
 reset()
 for n in parts:
  q=base[n].copy(); q[:,0]+=dx; q[:,1]+=dy
  for i,v in enumerate(objs[n].data.vertices): v.co=q[i]
def look(c,t): c.rotation_euler=(Vector(t)-c.location).to_track_quat('-Z','Y').to_euler()
def render(v):
 if v=='front': loc=(0,.35,.085);tgt=(0,.015,.078)
 elif v=='side': loc=(.35,0,.09);tgt=(0,.010,.080)
 else:
  r=(.20**2+.32**2)**.5;a=math.radians(28.25);loc=(r*math.sin(a),r*math.cos(a),.10);tgt=(0,.010,.080)
 bpy.ops.object.camera_add(location=loc);c=bpy.context.object;c.data.type='ORTHO';c.data.ortho_scale=.18;look(c,tgt);s.camera=c;s.render.resolution_x=sizes[v][0];s.render.resolution_y=sizes[v][1];s.render.resolution_percentage=100;tmp=OUT/f'_{v}.png';s.render.filepath=str(tmp);bpy.ops.render.render(write_still=True);bpy.data.objects.remove(c,do_unlink=True);im=bpy.data.images.load(str(tmp),check_existing=False);a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4);a=np.flipud(a);bpy.data.images.remove(im);return a[:,:,:3].max(2)>.03

def reg(mask,ref):
 y,x=np.where(mask);ry,rx=np.where(ref);y0,y1=y.min(),y.max()+1;x0,x1=x.min(),x.max()+1;R0,R1=ry.min(),ry.max()+1;C0,C1=rx.min(),rx.max()+1;crop=mask[y0:y1,x0:x1];sc=(R1-R0)/crop.shape[0];nw=max(1,round(crop.shape[1]*sc));yi=np.minimum(crop.shape[0]-1,(np.arange(R1-R0)/sc).astype(int));xi=np.minimum(crop.shape[1]-1,(np.arange(nw)/sc).astype(int));rs=crop[yi[:,None],xi[None,:]];pl=np.zeros_like(ref);xx=round((C0+C1-nw)/2);xa=max(0,xx);xb=min(ref.shape[1],xx+nw);pl[R0:R1,xa:xb]=rs[:,xa-xx:xa-xx+xb-xa];return float(np.logical_and(pl,ref).sum()/np.logical_or(pl,ref).sum())
def score(dx,dy):
 apply(dx,dy);d={v:reg(render(v),refs[v]) for v in sizes};d['mean']=sum(d.values())/3;d['obj']=.45*d['front']+.40*d['threequarter']+.15*d['side'];return d
best=(-1,0,0,None)
for dx in np.linspace(-.003,.003,9):
 for dy in np.linspace(-.003,.003,7):
  d=score(float(dx),float(dy));print('TRY',dx,dy,d,flush=True)
  if d['obj']>best[0]:best=(d['obj'],float(dx),float(dy),d)
print('BEST',best,flush=True);apply(best[1],best[2]);bpy.ops.wm.save_as_mainfile(filepath=str(P/'source/cat_fit_r32.blend'));(P/'reports/fit_r32_params.json').write_text(json.dumps({'dx':best[1],'dy':best[2],'score':best[3]},indent=2))