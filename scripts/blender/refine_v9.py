import bpy, math
# v9 from v8: remove heavy dark chin, sculpt two muzzle lobes, relax eye expression.
for o in bpy.data.objects:
    if o.type!='MESH': continue
    n=o.name
    if n.startswith('Sculpted_Head_Graphite'):
        for v in o.data.vertices:
            ax=abs(v.co.x)
            # tuck central lower jaw behind white muzzle
            if v.co.y>0.043 and v.co.z<0.108:
                wx=max(0.0,1.0-ax/0.017)
                wz=max(0.0,min(1.0,(0.108-v.co.z)/0.018))
                v.co.y -= 0.0030*wx*wz
                v.co.z += 0.0008*wx*wz
    elif n=='Sculpted_Head_Ivory':
        for v in o.data.vertices:
            ax=abs(v.co.x)
            # center groove; cheek lobes project more than philtrum
            center=max(0.0,1.0-ax/0.0045)
            lobe=math.exp(-((ax-0.0085)/0.0045)**2)
            front=max(0.0,min(1.0,(v.co.y-0.043)/0.012))
            v.co.y += front*(0.00115*lobe-0.00135*center)
            v.co.z += 0.00035*lobe
# Eyes/rims: outer corners slightly higher, eye centers 0.4 mm higher.
for o in bpy.data.objects:
    if o.type!='MESH' or not o.name.startswith('Inset_Eye_'): continue
    side=1 if 'Left' in o.name else -1
    xs=[v.co.x for v in o.data.vertices]
    cx=(min(xs)+max(xs))/2
    half=max((max(xs)-min(xs))/2,1e-6)
    for v in o.data.vertices:
        u=max(-1.0,min(1.0,(v.co.x-cx)/half))
        v.co.z += 0.0004 + 0.00075*side*u
rim=bpy.data.objects.get('Sculpted_Head_EyeRim')
if rim:
    half=0.0205
    for v in rim.data.vertices:
        side=1 if v.co.x>=0 else -1
        local=(abs(v.co.x)-0.0123)/0.0082
        local=max(-1.0,min(1.0,local))
        v.co.z += 0.00035 + 0.00055*local
# Slightly soften smile: keep corners only marginally lifted.
for name in ('Mouth_Left','Mouth_Right'):
    o=bpy.data.objects.get(name)
    if o:
        for v in o.data.vertices:
            ax=abs(v.co.x)
            v.co.z -= 0.00012*min(ax/0.004,1.0)
# preserve 150 mm max
for o in bpy.data.objects:
    if o.type=='MESH':
        for v in o.data.vertices:
            if v.co.z>0.150: v.co.z=0.150
out='/Users/ultra/xp/3d-cli/projects/low-poly-cat/source/cat_v9.blend'
bpy.ops.wm.save_as_mainfile(filepath=out)
bpy.ops.export_scene.gltf(filepath='/Users/ultra/xp/3d-cli/projects/low-poly-cat/exports/cat_v9.glb',export_format='GLB')
print('saved',out)