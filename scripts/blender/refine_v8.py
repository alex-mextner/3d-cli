import bpy, math
# v8 builds on v7: taper cheeks/jaw, compact muzzle, refine eye lens geometry.
for o in bpy.data.objects:
    if o.type!='MESH': continue
    n=o.name
    if n.startswith('Sculpted_Head_Graphite'):
        for v in o.data.vertices:
            z=v.co.z
            # narrower lower cheeks/jaw, retain temples/forehead
            low=max(0.0,min(1.0,(0.115-z)/0.024))
            v.co.x *= (1.0-0.085*low)
            # slightly taller crown without affecting chin
            high=max(0.0,min(1.0,(z-0.120)/0.020))
            v.co.z += 0.0011*high
            # reduce rear skull bubble only; face projection untouched
            if v.co.y<0.0268:
                v.co.y=0.0268+(v.co.y-0.0268)*0.965
    elif n=='Sculpted_Head_Ivory':
        for v in o.data.vertices:
            v.co.x*=0.94
            v.co.z=0.1035+(v.co.z-0.1035)*0.84+0.0007
            v.co.y+=0.00045
# Eye surfaces: shift outward slightly and sharpen outer/inner ends into a lens.
for o in bpy.data.objects:
    if o.type!='MESH' or not o.name.startswith('Inset_Eye_'): continue
    pupil=o.name.endswith('Pupil')
    side=1 if 'Left' in o.name else -1
    cx=side*0.01225
    xs=[abs(v.co.x-cx) for v in o.data.vertices]
    xmax=max(xs) if xs else 1
    for v in o.data.vertices:
        dx=v.co.x-cx
        if not pupil:
            # translate center outward while preserving current width
            v.co.x += side*0.0009
            u=min(abs(dx)/xmax,1.0)
            lens=1.06-0.34*(u**1.7)
            v.co.z=0.1188+(v.co.z-0.1188)*lens
        else:
            v.co.x += side*0.0009
            v.co.z=0.1188+(v.co.z-0.1188)*0.96
# Align black eye rim with shifted/lens-shaped eyes.
rim=bpy.data.objects.get('Sculpted_Head_EyeRim')
if rim:
    for v in rim.data.vertices:
        side=1 if v.co.x>=0 else -1
        v.co.x += side*0.00075
        u=min(abs(abs(v.co.x)-0.01225)/0.0105,1.0)
        v.co.z=0.1188+(v.co.z-0.1188)*(1.02-0.24*(u**1.5))
# Keep nose/mouth proportional to smaller muzzle.
nose=bpy.data.objects.get('Pink_Nose')
if nose:
    for v in nose.data.vertices:
        v.co.x*=0.95; v.co.z+=0.00055; v.co.y+=0.00035
for name in ('Mouth_Left','Mouth_Right'):
    o=bpy.data.objects.get(name)
    if o:
        for v in o.data.vertices:
            v.co.x*=0.92; v.co.z+=0.00065; v.co.y+=0.00035
# Clamp only accidental overshoot; preserve exact 150 mm ear tip height.
for o in bpy.data.objects:
    if o.type=='MESH':
        for v in o.data.vertices:
            if v.co.z>0.150: v.co.z=0.150
out='/Users/ultra/xp/3d-cli/projects/low-poly-cat/source/cat_v8.blend'
bpy.ops.wm.save_as_mainfile(filepath=out)
bpy.ops.export_scene.gltf(filepath='/Users/ultra/xp/3d-cli/projects/low-poly-cat/exports/cat_v8.glb',export_format='GLB')
print('saved',out)