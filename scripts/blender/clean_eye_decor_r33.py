import bpy
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
for n in ('Pupil_Left_DECOR','Pupil_Right_DECOR'):
 o=bpy.data.objects.get(n)
 if o: bpy.data.objects.remove(o,do_unlink=True)
# Narrow the pupil material band on the closed eye masters using face-centroid x around each lens center.
for n in ('Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER'):
 o=bpy.data.objects[n]; xs=[v.co.x for v in o.data.vertices]; cx=(min(xs)+max(xs))/2; pupil=next((i for i,m in enumerate(o.data.materials) if m.name=='Pupil'),1); iris=next((i for i,m in enumerate(o.data.materials) if m.name=='Iris'),0)
 half=(max(xs)-min(xs))*.115
 for f in o.data.polygons:
  fx=sum(o.data.vertices[i].co.x for i in f.vertices)/len(f.vertices); f.material_index=pupil if abs(fx-cx)<=half else iris
out=P/'source/cat_fit_r33.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); print('R33 eye decor removed, pupil halfwidth fraction .115')