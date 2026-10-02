import bpy
# v15: final proportion pass — outward ear tilt, softer lower chin, lighter eyelid rim.
for o in bpy.data.objects:
    if o.type!='MESH': continue
    n=o.name
    if n.startswith('Thick_Ear_'):
        side=1 if 'Left' in n else -1
        zs=[v.co.z for v in o.data.vertices]; z0,z1=min(zs),max(zs)
        for v in o.data.vertices:
            t=max(0.0,min(1.0,(v.co.z-z0)/max(z1-z0,1e-6)))
            v.co.x += side*(0.00155*t-0.00010*(1.0-t))
    elif n=='Sculpted_Head_Ivory':
        for v in o.data.vertices:
            if v.co.z<0.1018:
                v.co.x*=0.94
                v.co.y-=0.00055
                v.co.z+=0.00015
    elif n=='Sculpted_Head_EyeRim':
        for v in o.data.vertices:
            v.co.y-=0.00025
# Slightly raise outer eye corners without changing eye size.
for o in bpy.data.objects:
    if o.type=='MESH' and o.name.startswith('Inset_Eye_'):
        side=1 if 'Left' in o.name else -1
        xs=[v.co.x for v in o.data.vertices]; cx=(min(xs)+max(xs))/2
        half=max((max(xs)-min(xs))/2,1e-6)
        for v in o.data.vertices:
            u=(v.co.x-cx)/half
            v.co.z += 0.00028*side*u
for o in bpy.data.objects:
    if o.type=='MESH':
        for v in o.data.vertices:
            if v.co.z>0.150: v.co.z=0.150
out='/Users/ultra/xp/3d-cli/projects/low-poly-cat/source/cat_v15.blend'
bpy.ops.wm.save_as_mainfile(filepath=out)
bpy.ops.export_scene.gltf(filepath='/Users/ultra/xp/3d-cli/projects/low-poly-cat/exports/cat_v15.glb',export_format='GLB')
print('saved',out)