import bpy, os, sys
out='/Users/ultra/Downloads/cat_blender_work/low_poly_cat_150mm_blender_v6.usdz'
print('usd operators:', [x for x in dir(bpy.ops.wm) if 'usd' in x.lower()])
try:
    bpy.ops.wm.usd_export(filepath=out, export_materials=True)
except Exception as e:
    print('EXPORT_ERROR',repr(e)); raise
print('exists',os.path.exists(out),'size',os.path.getsize(out) if os.path.exists(out) else -1)