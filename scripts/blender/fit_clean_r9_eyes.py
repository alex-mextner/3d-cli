import bpy, math
from pathlib import Path
ROOT=Path('/Users/ultra/xp/3d-cli'); PROJ=ROOT/'projects/low-poly-cat'; iris=bpy.data.materials['Iris']; pupil=bpy.data.materials['Pupil']
centers={}
for name in ('Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER'):
    o=bpy.data.objects[name]; xs=[v.co.x for v in o.data.vertices]; ys=[v.co.y for v in o.data.vertices]; zs=[v.co.z for v in o.data.vertices]
    centers[name]=((min(xs)+max(xs))/2,(min(ys)+max(ys))/2+0.00155,(min(zs)+max(zs))/2); bpy.data.objects.remove(o,do_unlink=True)
for name,(cx,cy,cz) in centers.items():
    side=1 if cx>0 else -1
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16,ring_count=8,radius=1,location=(cx,cy,cz))
    eye=bpy.context.object; eye.name=name; eye.scale=(0.0048,0.0012,0.0027); eye.rotation_euler[1]=math.radians(-side*7.0)
    bpy.ops.object.transform_apply(location=False,rotation=True,scale=True); eye.data.materials.append(iris)
    for p in eye.data.polygons: p.use_smooth=False
    # Separate closed pupil lens: vertical in world space, very shallow, seated on the green lens surface.
    bpy.ops.mesh.primitive_uv_sphere_add(segments=12,ring_count=6,radius=1,location=(cx,cy+0.00135,cz))
    po=bpy.context.object; po.name=('Pupil_Left_DECOR' if side>0 else 'Pupil_Right_DECOR'); po.scale=(0.00058,0.00020,0.00215)
    bpy.ops.object.transform_apply(location=False,rotation=True,scale=True); po.data.materials.append(pupil)
    for p in po.data.polygons: p.use_smooth=False
out=PROJ/'source/cat_fit_r9.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); bpy.ops.export_scene.gltf(filepath=str(PROJ/'exports/cat_fit_r9.glb'),export_format='GLB')
print('FIT_R9_SAVED',out)