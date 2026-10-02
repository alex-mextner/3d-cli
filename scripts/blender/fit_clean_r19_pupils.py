import bpy, json
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
pupil=bpy.data.materials.get('Pupil'); iris=bpy.data.materials.get('Iris')
assert pupil and iris
for n in ('Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER'):
 o=bpy.data.objects[n]; me=o.data
 if pupil.name not in [m.name for m in me.materials]: me.materials.append(pupil)
 ii=next(i for i,m in enumerate(me.materials) if m.name=='Iris'); pi=next(i for i,m in enumerate(me.materials) if m.name=='Pupil')
 xs=[v.co.x for v in me.vertices]; ys=[v.co.y for v in me.vertices]; cx=(min(xs)+max(xs))/2; cy=(min(ys)+max(ys))/2; yr=(max(ys)-min(ys))/2
 for f in me.polygons:
  c=f.center; front=c.y>cy+0.05*yr; slit=abs(c.x-cx)<.00125
  f.material_index=pi if front and slit else ii
 print(n,'pupil_faces',sum(f.material_index==pi for f in me.polygons))
out=P/'source/cat_fit_r19.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); (P/'reports/fit_r19_params.json').write_text(json.dumps({'params':{'tq_angle':28.25}},indent=2)); print('FIT_R19_SAVED',out)