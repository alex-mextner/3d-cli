import bpy, numpy as np, json, math
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
def ar(n): return np.array([tuple(v.co) for v in bpy.data.objects[n].data.vertices],float)
def put(n,q):
 for i,v in enumerate(bpy.data.objects[n].data.vertices): v.co=q[i]
for n in ('Rounded_Torso_MASTER','Haunch_Left_MASTER','Haunch_Right_MASTER'):
 q=ar(n); c=(q.min(0)+q.max(0))/2; z=q[:,2]; w=np.exp(-((z-.043)/.020)**2); fx=1-.16*w; fy=1-.12*w; q[:,0]=c[0]+(q[:,0]-c[0])*fx; q[:,1]=c[1]+(q[:,1]-c[1])*fy; put(n,q)
# widen/lengthen low tail at z<20 mm to fill reference lower-left silhouette without widening haunches
n='Curled_tail_MASTER'; q=ar(n); c=(q.min(0)+q.max(0))/2; q[:,0]=c[0]+(q[:,0]-c[0])*1.12; put(n,q)
out=P/'source/cat_fit_r20.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); (P/'reports/fit_r20_params.json').write_text(json.dumps({'params':{'tq_angle':28.25}},indent=2)); print('FIT_R20_SAVED',out)