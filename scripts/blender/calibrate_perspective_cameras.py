import bpy, json, math, numpy as np
from pathlib import Path
from mathutils import Vector
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat'); OUT=P/'reports/camera_perspective_v1'; OUT.mkdir(parents=True,exist_ok=True)
s=bpy.context.scene; s.render.engine='BLENDER_WORKBENCH'; s.display.shading.light='FLAT'; s.display.shading.color_type='SINGLE'; s.display.shading.single_color=(1,1,1); s.display.shading.show_shadows=False; s.display.shading.show_cavity=False
sizes={'front':(133,256),'threequarter':(157,256),'side':(177,256)}; refs={}
for v in sizes:
 im=bpy.data.images.load(str(P/'refs'/f'mask_{v}.png'),check_existing=False); a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); refs[v]=np.flipud(a)[:,:,0]>.5; bpy.data.images.remove(im)
def look(c,t): c.rotation_euler=(Vector(t)-c.location).to_track_quat('-Z','Y').to_euler()
def iou(a,b): return float(np.logical_and(a,b).sum()/max(np.logical_or(a,b).sum(),1))
def bbox(m): y,x=np.where(m); return x.min(),y.min(),x.max()+1,y.max()+1
def render(v,p):
 w,h=sizes[v]; s.render.resolution_x=w; s.render.resolution_y=h; s.render.resolution_percentage=100; s.render.film_transparent=True
 target=Vector((0,.012,.080)); d=p['distance']
 if v=='front': loc=target+Vector((0,d,.005))
 elif v=='side': loc=target+Vector((d,0,.010))
 else:
  a=math.radians(p['angle']); loc=target+Vector((d*math.sin(a),d*math.cos(a),.020))
 bpy.ops.object.camera_add(location=loc); c=bpy.context.object; c.data.type='PERSP'; c.data.lens=p['lens']; c.data.sensor_fit='VERTICAL'; c.data.sensor_height=36; c.data.shift_x=p['shift_x']; c.data.shift_y=p['shift_y']; look(c,target); s.camera=c
 tmp=OUT/f'_tmp_{v}.png'; s.render.filepath=str(tmp); bpy.ops.render.render(write_still=True)
 im=bpy.data.images.load(str(tmp),check_existing=False); a=np.array(im.pixels[:],np.float32).reshape(im.size[1],im.size[0],4); a=np.flipud(a); bpy.data.images.remove(im); bpy.data.objects.remove(c,do_unlink=True); return a[:,:,3]>.5

def fit_frame(v,p):
 rb=bbox(refs[v]); target_h=rb[3]-rb[1]
 for _ in range(4):
  m=render(v,p); b=bbox(m); cur_h=b[3]-b[1]; p['lens']*=target_h/max(cur_h,1); p['lens']=max(20,min(240,p['lens']))
 # coordinate-descent image shifts only; no post-render registration
 best=iou(render(v,p),refs[v])
 for st in (.08,.04,.02,.01):
  for k in ('shift_x','shift_y'):
   base=p[k]; win=(best,base)
   for dlt in (-st,st):
    q=dict(p); q[k]=base+dlt; sc=iou(render(v,q),refs[v])
    if sc>win[0]: win=(sc,q[k])
   p[k]=win[1]; best=win[0]
 return best
result={'model':bpy.data.filepath,'projection':'PERSP','views':{}}
for v in ('front','threequarter','side'):
 best=None
 angles=[28.25] if v!='threequarter' else [24,27,30,33,36]
 for dist in (.30,.40,.50,.65,.85):
  for ang in angles:
   p={'distance':dist,'lens':100.0,'shift_x':0.0,'shift_y':0.0,'angle':ang}; sc=fit_frame(v,p); print('TRY',v,dist,ang,sc,p,flush=True)
   if best is None or sc>best[0]: best=(sc,dict(p))
 sc,p=best; m=render(v,p); rb=bbox(refs[v]); mb=bbox(m); result['views'][v]={'camera':p,'strict_iou':sc,'reference_bbox':[int(x) for x in rb],'render_bbox':[int(x) for x in mb]}
 # keep best exact-frame mask
 w,h=sizes[v]; s.render.resolution_x=w; s.render.resolution_y=h; s.render.film_transparent=True; render(v,p); (OUT/f'_tmp_{v}.png').rename(OUT/f'{v}_mask.png')
(P/'reports/locked_cameras_perspective_v1.json').write_text(json.dumps(result,indent=2)); print('PERSPECTIVE_RESULT',json.dumps(result),flush=True)
