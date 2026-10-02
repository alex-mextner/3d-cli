import bpy, math
# v7: correct skull aspect/volume first, then rebuild eye proportions and ear profile.
HEAD_CZ=0.115
HEAD_CY=0.0268
for o in bpy.data.objects:
    if o.type!='MESH': continue
    n=o.name
    if n.startswith('Sculpted_Head_Graphite'):
        for v in o.data.vertices:
            v.co.x *= 0.955
            v.co.z = HEAD_CZ + (v.co.z-HEAD_CZ)*1.135
            v.co.y = HEAD_CY + (v.co.y-HEAD_CY)*1.045
    elif n=='Sculpted_Head_Ivory':
        for v in o.data.vertices:
            v.co.x *= 0.91
            v.co.z = 0.103 + (v.co.z-0.103)*1.06
            v.co.y += 0.0018
    elif n=='Sculpted_Head_EyeRim':
        for v in o.data.vertices:
            v.co.x *= 1.03
            v.co.z = 0.117 + (v.co.z-0.117)*0.70 + 0.0015
            v.co.y += 0.0010
# Eyes: narrow feline almond, slightly higher and more forward.
for o in bpy.data.objects:
    if o.type!='MESH' or not o.name.startswith('Inset_Eye_'): continue
    is_pupil=o.name.endswith('Pupil')
    side=1 if 'Left' in o.name else -1
    cx=side*0.01135
    for v in o.data.vertices:
        v.co.x=cx+(v.co.x-cx)*(1.18 if not is_pupil else 0.88)
        v.co.z=0.117+(v.co.z-0.117)*(0.62 if not is_pupil else 0.68)+0.0018
        v.co.y += 0.00115
# Nose/mouth: smaller nose, fuller muzzle projection, slightly friendlier mouth.
for o in bpy.data.objects:
    if o.type!='MESH': continue
    if o.name=='Pink_Nose':
        for v in o.data.vertices:
            v.co.x*=0.90; v.co.z=0.1065+(v.co.z-0.1065)*0.92+0.0008; v.co.y+=0.0018
    elif o.name.startswith('Mouth_'):
        for v in o.data.vertices:
            v.co.x*=0.93; v.co.y+=0.0019
            v.co.z += 0.00045 + 0.00025*min(abs(v.co.x)/0.004,1.0)
# Ears: fix the previous wrong lean direction; broaden side profile at root and lean tips forward.
for o in bpy.data.objects:
    if o.type!='MESH' or not o.name.startswith('Thick_Ear_'): continue
    for v in o.data.vertices:
        t=max(0.0,min(1.0,(v.co.z-0.124)/0.026))
        # front is +Y
        v.co.y += 0.0036*t
        # roots remain buried in skull, tips keep their placement
        v.co.x *= (0.985 + 0.015*t)
        root_y=0.018
        v.co.y = root_y + (v.co.y-root_y)*(1.18-0.10*t)
        if t<0.35: v.co.z += 0.0012*(1.0-t/0.35)
# Keep exact height: ear tips remain <=150mm; floor unchanged.
for o in bpy.data.objects:
    if o.type=='MESH':
        for v in o.data.vertices:
            if v.co.z>0.150: v.co.z=0.150
out='/Users/ultra/xp/3d-cli/projects/low-poly-cat/source/cat_v7.blend'
bpy.ops.wm.save_as_mainfile(filepath=out)
bpy.ops.export_scene.gltf(filepath='/Users/ultra/xp/3d-cli/projects/low-poly-cat/exports/cat_v7.glb',export_format='GLB')
print('saved',out)