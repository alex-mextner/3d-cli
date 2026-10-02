import bpy, math
from mathutils import Vector
# Blender coordinates are meters; source model uses X width, Y depth, Z height.
# Ear correction: narrower root, forward lean in profile, preserve tip height.
for o in bpy.data.objects:
    if o.type!='MESH' or not o.name.startswith('Thick_Ear_'): continue
    for p in o.data.vertices:
        x,y,z=p.co
        t=max(0,min(1,(z-0.127)/0.023))
        s=1 if x>=0 else -1
        ax=abs(x)
        p.co.x=s*(ax*(0.94+0.035*t)-0.0012*(1-t))
        p.co.y=y-0.0024*t-0.0005*(1-t)
        p.co.z=z-0.0010*(1-t)
# Eye/head correction: open feline almond sockets and reduce heavy/downturned brow.
for o in bpy.data.objects:
    if o.type!='MESH' or not o.name.startswith('Sculpted_Head'): continue
    for p in o.data.vertices:
        x,y,z=p.co; ax=abs(x)
        wx=math.exp(-((ax-0.0125)/0.010)**4)
        wy=math.exp(-((z-0.1165)/0.008)**4)
        front=1/(1+math.exp((y-(-0.034))/0.0018))
        upper=math.exp(-((z-0.1180)/0.0030)**4)
        outer=math.exp(-((ax-0.0180)/0.0042)**4)*math.exp(-((z-0.116)/0.0045)**4)
        p.co.z += (0.00115*wx*upper + 0.00035*outer)*front
        p.co.y -= 0.00035*wx*wy*front
# Re-seat eye surfaces: larger almond iris, narrow vertical pupil, slightly forward in socket.
for o in bpy.data.objects:
    if o.type!='MESH' or not o.name.startswith('Inset_Eye_'): continue
    is_pupil=o.name.endswith('Pupil')
    for p in o.data.vertices:
        x,y,z=p.co; s=1 if x>=0 else -1; ax=abs(x)
        cx=0.0124
        if is_pupil:
            p.co.x=s*(cx+(ax-cx)*0.72)
            p.co.z=0.1162+(z-0.1162)*1.06
            p.co.y=y-0.00055
        else:
            p.co.x=s*(cx+(ax-cx)*1.08)
            p.co.z=0.1162+(z-0.1162)*1.08
            p.co.y=y-0.00065
# Normalize exact total height to 150 mm while preserving floor.
mesh=[o for o in bpy.data.objects if o.type=='MESH']
zs=[(o.matrix_world@v.co).z for o in mesh for v in o.data.vertices]
z0,z1=min(zs),max(zs); scale=0.150/(z1-z0)
for o in mesh:
    for p in o.data.vertices:
        p.co.z=(p.co.z-z0)*scale
# Save editable Blender master and export GLB.
out='/Users/ultra/Downloads/cat_blender_work/cat_v6.blend'
bpy.ops.wm.save_as_mainfile(filepath=out)
bpy.ops.export_scene.gltf(filepath='/Users/ultra/Downloads/cat_blender_work/cat_v6.glb',export_format='GLB')
print('saved',out,'height',scale*(z1-z0))