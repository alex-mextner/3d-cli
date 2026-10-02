import bpy
# v13: reduce rear-skull bulb, remove oversized white chin ring, soften eye rim.
for o in bpy.data.objects:
    if o.type!='MESH': continue
    n=o.name
    if n.startswith('Sculpted_Head_Graphite'):
        for v in o.data.vertices:
            if v.co.y < 0.024:
                v.co.y = 0.024 + (v.co.y-0.024)*0.88
    elif n=='Sculpted_Head_Ivory':
        for v in o.data.vertices:
            # Keep upper muzzle lobes; tuck broad lower chin behind graphite.
            if v.co.z < 0.1035:
                v.co.x *= 0.86
                v.co.y -= 0.0024
                v.co.z += 0.00045
    elif n=='Sculpted_Head_EyeRim':
        for v in o.data.vertices:
            side=1 if v.co.x>=0 else -1
            cx=side*0.0125
            v.co.x = cx + (v.co.x-cx)*0.92
            v.co.y -= 0.00075
# Bring eye objects slightly deeper again, preserving visible iris.
for o in bpy.data.objects:
    if o.type=='MESH' and o.name.startswith('Inset_Eye_'):
        for v in o.data.vertices: v.co.y -= 0.00045
for o in bpy.data.objects:
    if o.type=='MESH':
        for v in o.data.vertices:
            if v.co.z>0.150: v.co.z=0.150
out='/Users/ultra/xp/3d-cli/projects/low-poly-cat/source/cat_v13.blend'
bpy.ops.wm.save_as_mainfile(filepath=out)
bpy.ops.export_scene.gltf(filepath='/Users/ultra/xp/3d-cli/projects/low-poly-cat/exports/cat_v13.glb',export_format='GLB')
print('saved',out)