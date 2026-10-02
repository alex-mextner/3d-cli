import bpy
from pathlib import Path
P=Path('/Users/ultra/xp/3d-cli/projects/low-poly-cat')
for n in ('Thick_Ear_Left_MASTER','Thick_Ear_Right_MASTER'):
 o=bpy.data.objects[n]; zs=[v.co.z for v in o.data.vertices]; zmax=max(zs); ids=[i for i,v in enumerate(o.data.vertices) if zmax-v.co.z<0.0005]
 if len(ids)>=2:
  cx=sum(o.data.vertices[i].co.x for i in ids)/len(ids); cy=sum(o.data.vertices[i].co.y for i in ids)/len(ids)
  ids=sorted(ids,key=lambda i:o.data.vertices[i].co.x)
  for j,i in enumerate(ids):
   s=-1 if j < len(ids)/2 else 1; o.data.vertices[i].co.x=cx+s*0.00006; o.data.vertices[i].co.y=cy+s*0.00006
  print(n,'tip verts',ids,'collapsed to 0.12mm cap')
out=P/'source/cat_fit_r37.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(out)); print('R37 sharpened ear tips')