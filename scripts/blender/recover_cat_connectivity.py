"""Recover original face connectivity, NOT arbitrary hole-fill or appearance acceptance."""
import bpy, bmesh, math, json, sys
from collections import defaultdict
from pathlib import Path
from mathutils import Vector
sys.path.insert(0,str(Path(__file__).resolve().parent))
from qa_topology import audit_scene, part_key
ROOT=Path(__file__).resolve().parents[2]; PROJ=ROOT/'projects/low-poly-cat'
BASE=Path('/Users/ultra/Downloads/cat_blender_work/baseline_v5.blend')
DONOR=PROJ/'source/cat_v15.blend'
RESET_HEAD='--reset-head' in sys.argv
OUT=PROJ/('source/cat_clean_restart.blend' if RESET_HEAD else 'source/cat_recovery_topology.blend')

def snapshot(path):
    bpy.ops.wm.open_mainfile(filepath=str(path)); data={}; mats={}
    for o in bpy.context.scene.objects:
        if o.type!='MESH': continue
        slots=[]
        for m in o.data.materials:
            slots.append(m.name); node=next((n for n in m.node_tree.nodes if n.type=='BSDF_PRINCIPLED'),None) if m.use_nodes else None
            col=tuple(node.inputs['Base Color'].default_value) if node else tuple(m.diffuse_color)
            rough=float(node.inputs['Roughness'].default_value) if node else .7
            mats[m.name]=(col,rough)
        data[o.name]=dict(v=[tuple(o.matrix_world@v.co) for v in o.data.vertices],
                          f=[tuple(p.vertices) for p in o.data.polygons],
                          mi=[p.material_index for p in o.data.polygons],slots=slots)
    return data,mats

current,_=snapshot(DONOR); original,material_specs=snapshot(BASE)
if RESET_HEAD:
    current.update({n:o for n,o in original.items() if n.startswith(('Sculpted_Head','Thick_Ear','Inset_Eye','Pink_Nose','Mouth','Recessed'))})

def boundary_cycles(verts,faces):
    counts=defaultdict(int)
    for f in faces:
        for a,b in zip(f,f[1:]+f[:1]): counts[tuple(sorted((a,b)))]+=1
    adj=defaultdict(list)
    for (a,b),n in counts.items():
        if n==1: adj[a].append(b); adj[b].append(a)
    assert all(len(ns)==2 for ns in adj.values()),'Branched open edge; needs manual reconstruction'
    left=set(adj); cycles=[]
    while left:
        start=min(left); path=[start]; prev=None; at=start
        while True:
            nxt=next(n for n in adj[at] if n!=prev)
            if nxt==start: break
            assert nxt not in path,'Boundary repeats before closure'
            path.append(nxt); prev,at=at,nxt
        left.difference_update(path); cycles.append(path)
    return cycles

def recover(names,key):
    index={}; original_v=[]; candidates=defaultdict(list); faces=[]; face_mats=[]
    for name in names:
        src=original[name]; now=current.get(name); remap=[]
        if now is not None: assert len(src['v'])==len(now['v']),('Vertex correspondence lost',name)
        for i,v in enumerate(src['v']):
            q=tuple(round(x,7) for x in v)
            if q not in index: index[q]=len(original_v); original_v.append(v)
            j=index[q]; remap.append(j)
            if now is not None: candidates[j].append(Vector(now['v'][i]))
        for f,mi in zip(src['f'],src['mi']):
            faces.append(tuple(remap[i] for i in f)); face_mats.append(src['slots'][mi])
    assert all(i in candidates for i in range(len(original_v))),('Unsupported deleted vertex',key)
    positions=[sum(candidates[i],Vector())/len(candidates[i]) for i in range(len(original_v))]
    seam_moves=[max((p-positions[i]).length for p in candidates[i])*1000 for i in candidates]
    detail={'max_consensus_shift_mm':max(seam_moves),'restored_objects':[n for n in names if n not in current]}
    if key.startswith('Inset_Eye'):
        loops=boundary_cycles(original_v,faces)
        assert len(loops)==2 and len(loops[0])==len(loops[1]),'Unexpected eye seam topology'
        a,b=loops; n=len(a); options=[]
        for seq in (b,list(reversed(b))):
            for shift in range(n):
                pair=list(zip(a,seq[shift:]+seq[:shift]))
                dist=[(Vector(original_v[i])-Vector(original_v[j])).length for i,j in pair]
                options.append((sum(d*d for d in dist),max(dist),pair))
        _,error,pair=min(options,key=lambda x:x[0]); assert error < .003,'Eye seam mismatch exceeds 3 mm'
        aliases={j:i for i,j in pair}
        for i,j in pair: positions[i]=(positions[i]+positions[j])/2
        faces=[tuple(aliases.get(i,i) for i in f) for f in faces]
        detail['eye_seam_pairs']=n; detail['eye_seam_original_max_gap_mm']=error*1000
    return positions,faces,face_mats,detail

groups=defaultdict(list)
for name in original: groups[part_key(name)].append(name)
built={key:recover(names,key) for key,names in groups.items()}
bpy.ops.wm.read_factory_settings(use_empty=True)
materials={}
for name,(col,rough) in material_specs.items():
    mat=bpy.data.materials.new(name); mat.use_nodes=True; mat.diffuse_color=col
    bsdf=mat.node_tree.nodes.get('Principled BSDF'); bsdf.inputs['Base Color'].default_value=col; bsdf.inputs['Roughness'].default_value=rough
    materials[name]=mat
for key,(verts,faces,face_mats,detail) in built.items():
    used=sorted({i for f in faces for i in f}); remap={v:i for i,v in enumerate(used)}
    mesh=bpy.data.meshes.new(key+'_MASTER_mesh')
    mesh.from_pydata([verts[i] for i in used],[],[tuple(remap[i] for i in f) for f in faces]); mesh.update()
    names=list(dict.fromkeys(face_mats))
    for name in names: mesh.materials.append(materials[name])
    for face,name in zip(mesh.polygons,face_mats): face.material_index=names.index(name)
    bm=bmesh.new(); bm.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(bm,faces=list(bm.faces))
    if bm.calc_volume(signed=True)<0: bmesh.ops.reverse_faces(bm,faces=list(bm.faces))
    bm.to_mesh(mesh); bm.free()
    for f in mesh.polygons: f.use_smooth=key.startswith('Inset_Eye')
    obj=bpy.data.objects.new(key+'_MASTER',mesh); bpy.context.scene.collection.objects.link(obj)
    obj['status']='RECOVERY_SEED_NOT_REFERENCE_APPROVED'
bpy.context.scene.unit_settings.system='METRIC'; bpy.context.scene.unit_settings.scale_length=1.0
report=audit_scene(); report['recovery']={key:value[3] for key,value in built.items()}
report['cranial_geometry_reset_to_baseline']=RESET_HEAD; report['appearance_status']='NOT_APPROVED'; report['donor']=str(DONOR); report['connectivity_source']=str(BASE)
(PROJ/('reports/clean-restart-topology.json' if RESET_HEAD else 'reports/recovery-topology.json')).write_text(json.dumps(report,indent=2))
assert report['topology_ok'],'Recovery failed topology; do not save as usable seed'
bpy.ops.wm.save_as_mainfile(filepath=str(OUT))
print('RECOVERY_SEED_OK',len(report['parts']),'closed logical parts; appearance NOT approved')
