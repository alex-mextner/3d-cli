import bpy, math
from pathlib import Path
ROOT=Path('/Users/ultra/xp/3d-cli'); PROJ=ROOT/'projects/low-poly-cat'
iris=bpy.data.materials['Iris']; pupil=bpy.data.materials['Pupil']
centers={}
for name in ('Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER'):
    o=bpy.data.objects[name]; xs=[v.co.x for v in o.data.vertices]; ys=[v.co.y for v in o.data.vertices]; zs=[v.co.z for v in o.data.vertices]
    centers[name]=((min(xs)+max(xs))/2,(min(ys)+max(ys))/2+0.0018,(min(zs)+max(zs))/2)
    bpy.data.objects.remove(o,do_unlink=True)
# Rebuild each eye as a shallow closed low-poly lens. Material pupil is painted on faces, not separate protruding geometry.
for name,(cx,cy,cz) in centers.items():
    side=1 if cx>0 else -1
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2,radius=1,location=(cx,cy,cz))
    o=bpy.context.object; o.name=name; o.scale=(0.00445,0.00125,0.00265); o.rotation_euler[1]=math.radians(-side*7.0)
    bpy.ops.object.transform_apply(location=False,rotation=True,scale=True)
    o.data.materials.append(iris); o.data.materials.append(pupil)
    for p in o.data.polygons:
        c=sum((o.data.vertices[i].co for i in p.vertices), start=o.data.vertices[p.vertices[0]].co.copy()*0)
        c/=len(p.vertices); p.material_index=1 if abs(c.x)<0.00062 else 0
    for p in o.data.polygons: p.use_smooth=False
out=PROJ/'source/cat_fit_r7.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); bpy.ops.export_scene.gltf(filepath=str(PROJ/'exports/cat_fit_r7.glb'),export_format='GLB')
print('FIT_R7_SAVED',out)