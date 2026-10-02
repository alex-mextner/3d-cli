import bpy, numpy as np, json
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
def ar(n): return np.array([tuple(v.co) for v in bpy.data.objects[n].data.vertices],float)
def put(n,q):
 for i,v in enumerate(bpy.data.objects[n].data.vertices): v.co=q[i]
# Narrow mid/lower skull in X while preserving crown and muzzle.
n='Sculpted_Head_MASTER'; q=ar(n); z=q[:,2]; w=np.exp(-((z-.121)/.012)**2); q[:,0]*=(1-.12*w); put(n,q)
# Deepen ears in Y for side-view triangular profile without opening topology.
for n in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
 q=ar(n); c=(q.min(0)+q.max(0))/2; q[:,1]=(q[:,1]-c[1])*1.28+c[1]; put(n,q)
# Reduce body/haunch depth where side silhouette is consistently too wide.
for n,f in (('Rounded_Torso_MASTER',.88),('Haunch_Left_MASTER',.90),('Haunch_Right_MASTER',.90)):
 q=ar(n); c=(q.min(0)+q.max(0))/2; q[:,1]=(q[:,1]-c[1])*f+c[1]; put(n,q)
# Slightly tighten both haunch widths; extend tail laterally at its own center.
for n in ('Haunch_Left_MASTER','Haunch_Right_MASTER'):
 q=ar(n); c=(q.min(0)+q.max(0))/2; q[:,0]=(q[:,0]-c[0])*.93+c[0]; put(n,q)
n='Curled_tail_MASTER'; q=ar(n); c=(q.min(0)+q.max(0))/2; q[:,0]=(q[:,0]-c[0])*1.18+c[0]; put(n,q)
out=P/'source/cat_fit_r17.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); (P/'reports/fit_r17_params.json').write_text(json.dumps({'params':{'tq_angle':25.5}},indent=2)); print('FIT_R17_SAVED',out)