import bpy
# v12: reduce oversized skull while preserving the recovered vertical volume.
head_names=('Sculpted_Head_','Inset_Eye_','Pink_Nose','Mouth_','Recessed_Philtrum')
for o in bpy.data.objects:
    if o.type!='MESH': continue
    n=o.name
    if n.startswith(head_names):
        for v in o.data.vertices:
            # narrower and slightly less deep skull/face, minimal vertical change
            v.co.x *= 0.93
            v.co.y = 0.026 + (v.co.y-0.026)*0.955
            if n.startswith('Sculpted_Head_') and v.co.z<0.112:
                v.co.x *= 0.975
    if n.startswith('Thick_Ear_'):
        side=1 if 'Left' in n else -1
        xs=[v.co.x for v in o.data.vertices]; cx=(min(xs)+max(xs))/2
        for v in o.data.vertices:
            # wider triangular ear itself, but seat its center closer to skull
            v.co.x = cx + (v.co.x-cx)*1.07 - side*0.0011
# Keep eyes from becoming too close after skull narrowing.
for o in bpy.data.objects:
    if o.type=='MESH' and o.name.startswith('Inset_Eye_'):
        side=1 if 'Left' in o.name else -1
        for v in o.data.vertices: v.co.x += side*0.00065
# Compact white muzzle vertically a touch more.
iv=bpy.data.objects.get('Sculpted_Head_Ivory')
if iv:
    zs=[v.co.z for v in iv.data.vertices]; cz=(min(zs)+max(zs))/2
    for v in iv.data.vertices:
        v.co.z=cz+(v.co.z-cz)*0.92+0.00025
# Keep overall model height exact.
for o in bpy.data.objects:
    if o.type=='MESH':
        for v in o.data.vertices:
            if v.co.z>0.150: v.co.z=0.150
out='/Users/ultra/xp/3d-cli/projects/low-poly-cat/source/cat_v12.blend'
bpy.ops.wm.save_as_mainfile(filepath=out)
bpy.ops.export_scene.gltf(filepath='/Users/ultra/xp/3d-cli/projects/low-poly-cat/exports/cat_v12.glb',export_format='GLB')
print('saved',out)