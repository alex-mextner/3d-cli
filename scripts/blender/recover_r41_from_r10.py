import bpy, math
from mathutils import Vector
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
# Keep r10 body/head geometry. Only rebuild pupils as tiny closed vertical ellipsoids.
for o in list(bpy.context.scene.objects):
    if o.name.startswith('Pupil_') and o.name.endswith('_DECOR'):
        bpy.data.objects.remove(o,do_unlink=True)
for side in ('Left','Right'):
    eye=bpy.data.objects.get(f'Inset_Eye_{side}_MASTER')
    if not eye: continue
    c=eye.location.copy(); sign=1 if side=='Left' else -1
    # use current eye world center from vertices
    pts=[eye.matrix_world@v.co for v in eye.data.vertices]; c=sum(pts,Vector())/len(pts)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=6, location=(c.x,c.y+0.0012,c.z))
    p=bpy.context.object; p.name=f'Pupil_{side}_DECOR'; p.scale=(0.00072,0.00055,0.00335)
    mat=bpy.data.materials.get('Pupil')
    if mat: p.data.materials.append(mat)
    for poly in p.data.polygons: poly.use_smooth=False
bpy.ops.wm.save_as_mainfile(filepath=str(P/'source/cat_fit_r41.blend'))
print('R41 restored from r10 + clean pupils')