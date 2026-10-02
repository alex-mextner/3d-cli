import bpy,sys,json,numpy as np
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
args=sys.argv[sys.argv.index('--')+1:]; field=Path(args[0]); out=Path(args[1]); data=json.loads(field.read_text()); ts=np.array(data['t'],float)
objs=[o for o in bpy.context.scene.objects if o.type=='MESH' and not o.name.startswith(('REF_','QA_'))]
pts=np.vstack([[tuple(v.co) for v in o.data.vertices] for o in objs]); mn=pts.min(0); mx=pts.max(0); H=mx[2]-mn[2]; cxg=(mn[0]+mx[0])/2; cyg=(mn[1]+mx[1])/2
sx=np.array(data['x']['scale']); shx=np.array(data['x']['shiftH']); ccx=np.array(data['x']['current_centerH']); sy=np.array(data['y']['scale']); shy=np.array(data['y']['shiftH']); ccy=np.array(data['y']['current_centerH'])
for o in objs:
 for v in o.data.vertices:
  t=(mx[2]-v.co.z)/H; ax=float(np.interp(t,ts,sx)); dxh=float(np.interp(t,ts,shx)); cc=float(np.interp(t,ts,ccx)); cx=cxg-cc*H; v.co.x=cx+ax*(v.co.x-cx)-dxh*H
  ay=float(np.interp(t,ts,sy)); dyh=float(np.interp(t,ts,shy)); cc2=float(np.interp(t,ts,ccy)); cy=cyg+cc2*H; v.co.y=cy+ay*(v.co.y-cy)+dyh*H
bpy.ops.wm.save_as_mainfile(filepath=str(out)); print('DEFORM_APPLIED',field,'->',out,'H',H)