import bpy, numpy as np, json
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
def ar(n): return np.array([tuple(v.co) for v in bpy.data.objects[n].data.vertices],float)
def put(n,q):
 for i,v in enumerate(bpy.data.objects[n].data.vertices): v.co=q[i]
# Eyes: reference has taller almond lenses; keep them shallow and closed.
for n in ('Inset_Eye_Left_MASTER','Inset_Eye_Right_MASTER'):
 q=ar(n); c=(q.min(0)+q.max(0))/2; q[:,0]=(q[:,0]-c[0])*1.10+c[0]; q[:,2]=(q[:,2]-c[2])*1.30+c[2]; put(n,q)
# Muzzle: tighten white lobes by moving all vertices incident to Ivory material faces inward in X.
o=bpy.data.objects['Sculpted_Head_MASTER']; mesh=o.data; ivory=next((i for i,m in enumerate(mesh.materials) if m and m.name=='Ivory'),None)
ids=set()
if ivory is not None:
 for f in mesh.polygons:
  if f.material_index==ivory: ids.update(f.vertices)
q=ar('Sculpted_Head_MASTER')
for i in ids: q[i,0]*=.90
put('Sculpted_Head_MASTER',q)
# Side-view ears: lean tips forward (+Y), leaving roots seated in skull.
for n in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
 q=ar(n); t=np.clip((q[:,2]-.124)/.026,0,1); q[:,1]+=.0034*t; put(n,q)
out=P/'source/cat_fit_r18.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); (P/'reports/fit_r18_params.json').write_text(json.dumps({'params':{'tq_angle':28.25}},indent=2)); print('FIT_R18_SAVED',out)