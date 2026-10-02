import bpy, math
from pathlib import Path
ROOT=Path('/Users/ultra/xp/3d-cli'); PROJ=ROOT/'projects/low-poly-cat'; iris=bpy.data.materials['Iris']; pupil=bpy.data.materials['Pupil']
centers={}
for name in ('Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER'):
    o=bpy.data.objects[name]; xs=[v.co.x for v in o.data.vertices]; ys=[v.co.y for v in o.data.vertices]; zs=[v.co.z for v in o.data.vertices]
    centers[name]=((min(xs)+max(xs))/2,(min(ys)+max(ys))/2+0.00155,(min(zs)+max(zs))/2); bpy.data.objects.remove(o,do_unlink=True)
# UV-sphere topology gives stable vertical meridians for a true slit pupil.
for name,(cx,cy,cz) in centers.items():
    side=1 if cx>0 else -1
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16,ring_count=8,radius=1,location=(cx,cy,cz))
    o=bpy.context.object; o.name=name; o.scale=(0.00485,0.00120,0.00270); o.rotation_euler[1]=math.radians(-side*7.0)
    bpy.ops.object.transform_apply(location=False,rotation=True,scale=True); o.data.materials.append(iris); o.data.materials.append(pupil)
    for p in o.data.polygons:
        x=sum(o.data.vertices[i].co.x for i in p.vertices)/len(p.vertices); p.material_index=1 if abs(x)<0.00072 else 0; p.use_smooth=False
out=PROJ/'source/cat_fit_r8.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); bpy.ops.export_scene.gltf(filepath=str(PROJ/'exports/cat_fit_r8.glb'),export_format='GLB')
print('FIT_R8_SAVED',out)