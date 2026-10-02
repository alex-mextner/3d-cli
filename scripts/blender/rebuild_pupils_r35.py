import bpy
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
# Remove prior pupil decorations and make the closed eye lenses iris-only.
for n in list(bpy.data.objects.keys()):
 if n.startswith('Pupil_'): bpy.data.objects.remove(bpy.data.objects[n],do_unlink=True)
for side in ('Left','Right'):
 eye=bpy.data.objects[f'Inset_Eye_{side}_MASTER']; iris=next((i for i,m in enumerate(eye.data.materials) if m.name=='Iris'),0)
 for f in eye.data.polygons: f.material_index=iris
 vs=[eye.matrix_world@v.co for v in eye.data.vertices]; xs=[v.x for v in vs]; ys=[v.y for v in vs]; zs=[v.z for v in vs]
 center=((min(xs)+max(xs))/2,max(ys)+0.00022,(min(zs)+max(zs))/2)
 bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=6, radius=1, location=center)
 o=bpy.context.object; o.name=f'Pupil_{side}_MASTER'; o.scale=(0.00072,0.00032,0.00315); bpy.ops.object.transform_apply(location=False,rotation=False,scale=True)
 mat=bpy.data.materials.get('Pupil')
 if mat: o.data.materials.append(mat)
 for p in o.data.polygons: p.use_smooth=False
out=P/'source/cat_fit_r35.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); print('R35 pupils rebuilt as closed slit ellipsoids')