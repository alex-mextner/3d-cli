import bpy, json, math, sys, numpy as np
from pathlib import Path
from mathutils import Vector
ROOT=Path('/Users/ultra/xp/3d-cli'); P=ROOT/'projects/low-poly-cat'; OUT=P/'reports/camera_calibration_v1'; OUT.mkdir(parents=True,exist_ok=True)
s=bpy.context.scene; s.render.engine='BLENDER_WORKBENCH'; s.display.shading.light='FLAT'; s.display.shading.color_type='SINGLE'; s.display.shading.single_color=(1,1,1); s.display.shading.show_shadows=False; s.display.shading.show_cavity=False
sizes={'front':(133,256),'threequarter':(157,256),'side':(177,256)}
refs={}
for v in sizes:
 im=bpy.data.images.load(str(P/'refs'/f'mask_{v}.png'),check_existing=False); a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); refs[v]=np.flipud(a)[:,:,0]>.5; bpy.data.images.remove(im)

def look(c,t): c.rotation_euler=(Vector(t)-c.location).to_track_quat('-Z','Y').to_euler()
def mask_bbox(m):
 y,x=np.where(m); return (x.min(),y.min(),x.max()+1,y.max()+1)
def iou(a,b): return float(np.logical_and(a,b).sum()/max(np.logical_or(a,b).sum(),1))
def render_mask(v,p,save=False):
 w,h=sizes[v]; s.render.resolution_x=w; s.render.resolution_y=h; s.render.resolution_percentage=100; s.render.film_transparent=True
 if v=='front': loc=(0,.35,.085); target=(0,.015,.078)
 elif v=='side': loc=(.35,0,.09); target=(0,.010,.080)
 else:
  r=(.20**2+.32**2)**.5; a=math.radians(p['angle']); loc=(r*math.sin(a),r*math.cos(a),.10); target=(0,.010,.080)
 bpy.ops.object.camera_add(location=loc); c=bpy.context.object; c.data.type='ORTHO'; c.data.ortho_scale=p['scale']; c.data.shift_x=p['shift_x']; c.data.shift_y=p['shift_y']; look(c,target); s.camera=c
 tmp=OUT/f'_tmp_{v}.png'; s.render.filepath=str(tmp); bpy.ops.render.render(write_still=True)
 im=bpy.data.images.load(str(tmp),check_existing=False); a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); a=np.flipud(a); bpy.data.images.remove(im); bpy.data.objects.remove(c,do_unlink=True)
 m=a[:,:,3]>.5
 if save: tmp.rename(OUT/f'{v}_mask.png')
 return m

def score(v,p): return iou(render_mask(v,p),refs[v])
def calibrate(v):
 p={'scale':.18,'shift_x':0.,'shift_y':0.,'angle':28.25}; best=score(v,p); print('INIT',v,best,p,flush=True)
 steps={'scale':.012,'shift_x':.055,'shift_y':.055};
 if v=='threequarter': steps['angle']=3.0
 for rnd in range(5):
  for k,st in list(steps.items()):
   base=p[k]; win=(best,base)
   for d in (-st,st):
    q=dict(p); q[k]=base+d
    if k=='scale': q[k]=max(.12,min(.25,q[k]))
    sc=score(v,q); print('TRY',rnd,v,k,q[k],sc,flush=True)
    if sc>win[0]: win=(sc,q[k])
   p[k]=win[1]; best=win[0]
  for k in steps: steps[k]*=.5
 return p,best
result={'model':bpy.data.filepath,'projection':'ORTHO','views':{}}
for v in ('front','threequarter','side'):
 p,sc=calibrate(v); m=render_mask(v,p,save=True); result['views'][v]={'camera':p,'strict_iou':sc,'reference_bbox':[int(x) for x in mask_bbox(refs[v])],'render_bbox':[int(x) for x in mask_bbox(m)]}
(P/'reports/locked_cameras_v1.json').write_text(json.dumps(result,indent=2))
print('CAMERAS_LOCKED_V1',json.dumps(result),flush=True)
