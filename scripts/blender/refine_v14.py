import bpy
# v14 from v12: remove duplicate ear mid-shells that appear as extra fins in profile.
for name in ('Thick_Ear_Left_GraphiteMid','Thick_Ear_Right_GraphiteMid'):
    o=bpy.data.objects.get(name)
    if o: bpy.data.objects.remove(o,do_unlink=True)
# Slightly shorten rear skull without flattening the crown.
for o in bpy.data.objects:
    if o.type=='MESH' and o.name.startswith('Sculpted_Head_Graphite'):
        for v in o.data.vertices:
            if v.co.y<0.022:
                v.co.y=0.022+(v.co.y-0.022)*0.95
# Reduce only the lower white chin; keep the two muzzle lobes intact.
iv=bpy.data.objects.get('Sculpted_Head_Ivory')
if iv:
    for v in iv.data.vertices:
        if v.co.z<0.1015:
            v.co.x*=0.92
            v.co.y-=0.00085
            v.co.z+=0.00020
rim=bpy.data.objects.get('Sculpted_Head_EyeRim')
if rim:
    for v in rim.data.vertices: v.co.y-=0.00020
for o in bpy.data.objects:
    if o.type=='MESH':
        for v in o.data.vertices:
            if v.co.z>0.150: v.co.z=0.150
out='/Users/ultra/xp/3d-cli/projects/low-poly-cat/source/cat_v14.blend'
bpy.ops.wm.save_as_mainfile(filepath=out)
bpy.ops.export_scene.gltf(filepath='/Users/ultra/xp/3d-cli/projects/low-poly-cat/exports/cat_v14.glb',export_format='GLB')
print('saved',out)