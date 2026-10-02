import bpy
# v11: smaller, farther-apart feline eyes and a more compact muzzle.
for o in bpy.data.objects:
    if o.type!='MESH': continue
    n=o.name
    if n.startswith('Inset_Eye_'):
        side=1 if 'Left' in n else -1
        xs=[v.co.x for v in o.data.vertices]; zs=[v.co.z for v in o.data.vertices]
        cx=(min(xs)+max(xs))/2; cz=(min(zs)+max(zs))/2
        for v in o.data.vertices:
            v.co.x = cx + (v.co.x-cx)*0.84 + side*0.0010
            v.co.z = cz + (v.co.z-cz)*0.92
            v.co.y -= 0.00025
    elif n=='Sculpted_Head_Ivory':
        zs=[v.co.z for v in o.data.vertices]; cz=(min(zs)+max(zs))/2
        for v in o.data.vertices:
            v.co.x *= 0.91
            v.co.z = cz + (v.co.z-cz)*0.88 + 0.00055
            if v.co.y>0.047: v.co.y -= 0.00055
    elif n.startswith('Sculpted_Head_Graphite'):
        for v in o.data.vertices:
            if v.co.z<0.113: v.co.x *= 0.982
            if v.co.z<0.107 and v.co.y>0.042:
                v.co.y -= 0.0017*max(0.0,1.0-abs(v.co.x)/0.018)
rim=bpy.data.objects.get('Sculpted_Head_EyeRim')
if rim:
    for v in rim.data.vertices:
        side=1 if v.co.x>=0 else -1
        cx=side*0.0123
        v.co.x = cx + (v.co.x-cx)*0.86 + side*0.0009
        v.co.y -= 0.00035
        v.co.z = 0.118 + (v.co.z-0.118)*0.91
nose=bpy.data.objects.get('Pink_Nose')
if nose:
    zs=[v.co.z for v in nose.data.vertices]; cz=(min(zs)+max(zs))/2
    for v in nose.data.vertices:
        v.co.x*=0.92
        v.co.z=cz+(v.co.z-cz)*0.93+0.00035
# exact 150 mm ceiling
for o in bpy.data.objects:
    if o.type=='MESH':
        for v in o.data.vertices:
            if v.co.z>0.150: v.co.z=0.150
out='/Users/ultra/xp/3d-cli/projects/low-poly-cat/source/cat_v11.blend'
bpy.ops.wm.save_as_mainfile(filepath=out)
bpy.ops.export_scene.gltf(filepath='/Users/ultra/xp/3d-cli/projects/low-poly-cat/exports/cat_v11.glb',export_format='GLB')
print('saved',out)