import bpy, math
# v10: fix ear profile, recess eyes, and refine head/muzzle proportions.
for o in bpy.data.objects:
    if o.type!='MESH': continue
    n=o.name
    if n.startswith('Thick_Ear_'):
        zs=[v.co.z for v in o.data.vertices]; z0,z1=min(zs),max(zs)
        ys=[v.co.y for v in o.data.vertices]; yc=(min(ys)+max(ys))/2
        side=1 if 'Left' in n else -1
        for v in o.data.vertices:
            t=max(0.0,min(1.0,(v.co.z-z0)/max(z1-z0,1e-6)))
            # side view must taper to a thin forward-pointing wedge
            v.co.y = yc + (v.co.y-yc)*(1.0-0.72*t) + 0.0036*t
            # pull both ears inward without changing their triangular width much
            v.co.x -= side*(0.0014*(0.35+0.65*t))
    elif n.startswith('Sculpted_Head_Graphite'):
        for v in o.data.vertices:
            ax=abs(v.co.x)
            # less spherical lower cheek, slightly cleaner crown-to-cheek taper
            if v.co.z < 0.119:
                f=max(0.0,min(1.0,(0.119-v.co.z)/0.026))
                v.co.x *= 1.0-0.035*f
            # tuck central dark chin further behind white muzzle
            if v.co.y>0.043 and v.co.z<0.108:
                wx=max(0.0,1.0-ax/0.018)
                v.co.y -= 0.00135*wx
# Recess and slightly flatten the eyes so they sit inside the sockets.
for o in bpy.data.objects:
    if o.type!='MESH' or not o.name.startswith('Inset_Eye_'): continue
    is_pupil=o.name.endswith('Pupil')
    xs=[v.co.x for v in o.data.vertices]; zs=[v.co.z for v in o.data.vertices]
    cx=(min(xs)+max(xs))/2; cz=(min(zs)+max(zs))/2
    for v in o.data.vertices:
        v.co.y -= 0.00115 if not is_pupil else 0.00125
        v.co.x = cx + (v.co.x-cx)*(0.96 if not is_pupil else 0.84)
        v.co.z = cz + (v.co.z-cz)*(0.88 if not is_pupil else 0.94)
# Push the dark eye rim back a little to reduce the heavy eyeliner look.
rim=bpy.data.objects.get('Sculpted_Head_EyeRim')
if rim:
    for v in rim.data.vertices:
        v.co.y -= 0.00065
# Compact muzzle and nose.
iv=bpy.data.objects.get('Sculpted_Head_Ivory')
if iv:
    for v in iv.data.vertices:
        v.co.x *= 0.975
nose=bpy.data.objects.get('Pink_Nose')
if nose:
    for v in nose.data.vertices:
        v.co.x *= 0.94
# Clamp exact max height.
for o in bpy.data.objects:
    if o.type=='MESH':
        for v in o.data.vertices:
            if v.co.z>0.150: v.co.z=0.150
out='/Users/ultra/xp/3d-cli/projects/low-poly-cat/source/cat_v10.blend'
bpy.ops.wm.save_as_mainfile(filepath=out)
bpy.ops.export_scene.gltf(filepath='/Users/ultra/xp/3d-cli/projects/low-poly-cat/exports/cat_v10.glb',export_format='GLB')
print('saved',out)