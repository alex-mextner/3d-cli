"""Read-only logical-solid audit; run inside Blender with --python-exit-code 1."""
import bpy, bmesh, json, sys, re, math
from collections import defaultdict
from pathlib import Path
from mathutils.bvhtree import BVHTree
TOL = 1e-7  # meters: 0.0001 mm, only weld coincident material seams

def part_key(name):
    for prefix in ('Sculpted_Head','Rounded_Torso','Curled_tail'):
        if name.startswith(prefix): return prefix
    match = re.match(r'((?:Thick_Ear|Inset_Eye|Front_paw|Hind_paw|Haunch|Foreleg)_(?:Left|Right))', name)
    return match.group(1) if match else name

def audit_objects(objects):
    verts, faces = [], []
    for obj in objects:
        start = len(verts)
        verts.extend(tuple(obj.matrix_world @ v.co) for v in obj.data.vertices)
        faces.extend(tuple(start+i for i in f.vertices) for f in obj.data.polygons)
    mesh = bpy.data.meshes.new('QA_TEMP')
    mesh.from_pydata(verts, [], faces); mesh.update()
    bm = bmesh.new(); bm.from_mesh(mesh); bpy.data.meshes.remove(mesh)
    original_faces = len(bm.faces)
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=TOL)
    bm.normal_update(); bm.verts.ensure_lookup_table(); bm.verts.index_update()
    boundary = [e for e in bm.edges if e.is_boundary]
    bad_degree = [e for e in bm.edges if len(e.link_faces) != 2]
    zero_faces = sum(f.calc_area() < 1e-14 for f in bm.faces)
    inconsistent = sum(e.is_manifold and not e.is_contiguous for e in bm.edges)
    pending = set(bm.verts); components = 0
    while pending:
        todo = [pending.pop()]; components += 1
        while todo:
            v = todo.pop()
            for e in v.link_edges:
                other = e.other_vert(v)
                if other in pending: pending.remove(other); todo.append(other)
    tri_bm=bm.copy(); bmesh.ops.triangulate(tri_bm,faces=list(tri_bm.faces))
    tri_bm.verts.ensure_lookup_table(); tri_bm.verts.index_update()
    triangles=[tuple(v.index for v in f.verts) for f in tri_bm.faces]
    tree=BVHTree.FromPolygons([v.co for v in tri_bm.verts],triangles,all_triangles=True,epsilon=0)
    overlaps={tuple(sorted((a,b))) for a,b in tree.overlap(tree) if a!=b and not(set(triangles[a])&set(triangles[b]))}
    tri_bm.free()
    volume = bm.calc_volume(signed=True)
    finite = all(math.isfinite(x) for v in bm.verts for x in v.co)
    out = dict(objects=[o.name for o in objects], vertices=len(bm.verts),
               faces=len(bm.faces), faces_before_weld=original_faces,
               boundary_edges=len(boundary), non_two_face_edges=len(bad_degree),
               inconsistent_winding_edges=int(inconsistent), degenerate_faces=zero_faces,
               components=components, signed_volume_mm3=volume*1e9, finite=finite,
               boundary_length_mm=sum(e.calc_length() for e in boundary)*1000,
               boundary_segments_mm=[[list(a.co*1000),list(b.co*1000)] for a,b in [e.verts for e in boundary]])
    out['nonadjacent_overlap_candidates'] = len(overlaps)
    out['weld_removed_faces'] = original_faces - len(bm.faces)
    out['topology_ok'] = bool(finite and not bad_degree and not zero_faces and not inconsistent and volume > 0 and components == 1 and out['nonadjacent_overlap_candidates'] == 0 and out['weld_removed_faces'] == 0)
    bm.free(); return out

def audit_scene():
    groups = defaultdict(list)
    for o in bpy.context.scene.objects:
        if o.type=='MESH' and not o.name.startswith(('REF_','QA_')): groups[part_key(o.name)].append(o)
    parts = {key:audit_objects(obs) for key,obs in groups.items()}
    return dict(source=bpy.data.filepath, weld_tolerance_mm=TOL*1000, parts=parts,
                topology_ok=bool(parts) and all(p['topology_ok'] for p in parts.values()),
                scope='Closed logical shells after coincident seam weld; does NOT certify self-intersections, thickness, print union, appearance or reference match.')

def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',required=True)
    parser.add_argument('--fail',action='store_true')
    args=parser.parse_args(sys.argv[sys.argv.index('--')+1:])
    report=audit_scene()
    out=Path(args.out); out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2))
    for key,p in report['parts'].items():
        if not p['topology_ok'] or key.startswith(('Sculpted','Thick','Inset')):
            print('QA',key,'boundary=',p['boundary_edges'],'non2=',p['non_two_face_edges'],
                  'winding=',p['inconsistent_winding_edges'],'faces=',p['faces'],'components=',p['components'])
    print('TOPOLOGY_OK',report['topology_ok'],'REPORT',out)
    if args.fail and not report['topology_ok']: raise RuntimeError('Topology gate failed; release export prohibited')

if __name__=='__main__': main()
