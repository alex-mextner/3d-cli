import bpy
for o in bpy.data.objects:
 if o.type!='MESH' or not o.name.startswith(('Sculpted_Head','Inset_Eye','Thick_Ear','Pink_Nose')): continue
 vs=[o.matrix_world@v.co for v in o.data.vertices]
 mi=[min(v[i] for v in vs) for i in range(3)]; ma=[max(v[i] for v in vs) for i in range(3)]
 print(o.name,[round((ma[i]-mi[i])*1000,2) for i in range(3)],[round(x*1000,2) for x in mi],[round(x*1000,2) for x in ma])