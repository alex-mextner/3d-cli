"""Only closed/screened meshes may be packaged; final export also needs reference approval."""
import bpy,sys,json,hashlib,argparse,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from qa_topology import audit_scene
ROOT=Path(__file__).resolve().parents[2]; PROJ=ROOT/'projects/low-poly-cat'
p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--diagnostic',action='store_true')
a=p.parse_args(sys.argv[sys.argv.index('--')+1:]);out=Path(a.output)
if out.exists(): raise RuntimeError('Refusing to overwrite an existing artifact')
report=audit_scene()
if not report['topology_ok']: raise RuntimeError('EXPORT BLOCKED: open, degenerate, disconnected, inverted or intersecting logical shell')
if a.diagnostic:
    if not out.name.startswith('diagnostic_'): raise RuntimeError('Diagnostic exports must be named diagnostic_*')
else:
    approval=PROJ/'reports/reference-approval.json'
    if not approval.is_file(): raise RuntimeError('EXPORT BLOCKED: reference approval is missing')
    approved=json.loads(approval.read_text())
    sha=hashlib.sha256(Path(bpy.data.filepath).read_bytes()).hexdigest()
    if approved.get('model_sha256')!=sha or approved.get('status')!='passed':
        raise RuntimeError('EXPORT BLOCKED: approval does not match this Blender checkpoint')
    for view in ('front','threequarter','side'):
        v=approved.get('views',{}).get(view,{})
        if v.get('status')!='passed': raise RuntimeError('EXPORT BLOCKED: '+view+' did not pass')
        for role in ('reference','render','overlay','metrics'):
            evidence=v.get(role,{})
            file=PROJ/evidence.get('path','MISSING')
            if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=evidence.get('sha256'):
                raise RuntimeError('EXPORT BLOCKED: missing/stale evidence '+view+'/'+role)

bpy.ops.object.select_all(action='DESELECT')
for o in bpy.context.scene.objects:
    if o.type=='MESH' and not o.name.startswith(('REF_','QA_')): o.select_set(True)
props=bpy.ops.wm.usd_export.get_rna_type().properties.keys()
if 'selected_objects_only' not in props: raise RuntimeError('USD exporter lacks required selection gate')
out.parent.mkdir(parents=True,exist_ok=True)
tmp=out.with_name('.pending_'+out.name)
if tmp.exists(): raise RuntimeError('Stale staging file exists; inspect before retrying')
bpy.ops.wm.usd_export(filepath=str(tmp),selected_objects_only=True,export_materials=True)
with zipfile.ZipFile(tmp) as z:
    if z.testzip() is not None: raise RuntimeError('USDZ ZIP CRC failed')
    if any(i.compress_type!=zipfile.ZIP_STORED for i in z.infolist()): raise RuntimeError('USDZ must be uncompressed')
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.usd_import(filepath=str(tmp))
roundtrip=audit_scene()
if not roundtrip['topology_ok']: raise RuntimeError('USDZ native round-trip topology failed')
if len(roundtrip['parts'])!=len(report['parts']): raise RuntimeError('USDZ round-trip lost logical parts')
(tmp.with_suffix('.validation.json')).write_text(json.dumps({'diagnostic_only':a.diagnostic,'native_blender_usdz_roundtrip':True,'topology':roundtrip},indent=2))
tmp.rename(out)
print('GUARDED_EXPORT_OK',out,'DIAGNOSTIC_ONLY',a.diagnostic)
