import bpy
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
for side in ('Left','Right'):
 eye=bpy.data.objects[f'Inset_Eye_{side}_MASTER']; xs=[v.co.x for v in eye.data.vertices]; zs=[v.co.z for v in eye.data.vertices]; cx=(min(xs)+max(xs))/2; cz=(min(zs)+max(zs))/2
 for v in eye.data.vertices:
  v.co.x=cx+(v.co.x-cx)*1.04; v.co.z=cz+(v.co.z-cz)*0.84
 pupil=bpy.data.objects.get(f'Pupil_{side}_MASTER')
 if pupil:
  xs=[v.co.x for v in pupil.data.vertices]; zs=[v.co.z for v in pupil.data.vertices]; cx=(min(xs)+max(xs))/2; cz=(min(zs)+max(zs))/2
  for v in pupil.data.vertices:
   v.co.x=cx+(v.co.x-cx)*0.92; v.co.z=cz+(v.co.z-cz)*0.84
out=P/'source/cat_fit_r36.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); print('R36 eyes flattened to feline almond proportions')