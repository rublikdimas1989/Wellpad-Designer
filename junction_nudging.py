"""Conservative relocation of external trunk taps toward an existing trunk plate.

Only moves the end of a vertical lead and its preceding horizontal lead;
this retains the original number of bends and creates an ordinary three-port
tee on the same physical trunk instead of adding a plate through the trunk.
"""
from dataclasses import replace


def _match(a,b,eps=1e-5):
    return abs(a.u-b.u)<eps and abs(a.v-b.v)<eps


def _trunk_contains(seg, u, v, margin=.05):
    from axial_plates import TRUNK_PURPOSES
    return (seg.purpose in TRUNK_PURPOSES and seg.horizontal
            and abs(seg.a.v-v)<1e-5 and
            min(seg.a.u,seg.b.u)+margin < u < max(seg.a.u,seg.b.u)-margin)


def move_taps_to_existing_trunk_slabs(segments, slabs, placements=(), max_shift=1.5):
    """Return (segments, moves); never introduce another bend or a 4-way tee."""
    from routing import Pt, Segment, protected_well_conflicts, forbidden_foreign_contacts
    from topology_rules import four_way_nodes
    from network_cost import network_fittings

    lines=list(segments)
    notes=[]
    for _ in range(10):
        moved=False
        for i,branch in enumerate(lines):
            if branch.horizontal or branch.length < .8:
                continue
            purpose=branch.purpose.casefold()
            if any(q in purpose for q in ('пожарного узла','хребет','отвод от скв. ',
                                           'плита','замер')):
                continue
            for junction, other in ((branch.a,branch.b),(branch.b,branch.a)):
                if not any(_trunk_contains(t,junction.u,junction.v) and t.medium==branch.medium
                           for t in lines):
                    continue
                # Need upstream horizontal leg to shift the tee without
                # introducing two new bends.
                prev_options=[(j,t) for j,t in enumerate(lines)
                              if j!=i and t.medium==branch.medium and t.phase==branch.phase
                              and t.purpose==branch.purpose and t.horizontal and
                              (_match(t.a,other) or _match(t.b,other)) and t.length > .8]
                if not prev_options:
                    continue
                for j,prev in prev_options:
                    far=prev.b if _match(prev.a,other) else prev.a
                    choices=[]
                    for slab in slabs:
                        if slab.slab_type not in ('axial_junction','trunk_intermediate','central'):
                            continue
                        if not (slab.v_min-1e-5<=junction.v<=slab.v_max+1e-5):
                            continue
                        if branch.medium not in slab.media:
                            continue
                        if slab.u_start<=junction.u<=slab.u_end:
                            continue
                        candidate=(slab.u_start+.12 if junction.u<slab.u_start
                                   else slab.u_end-.12)
                        shift=abs(candidate-junction.u)
                        if .01 < shift <= max_shift:
                            choices.append((shift,candidate))
                    for distance,new_u in sorted(choices):
                        # Don't shrink upstream horizontal leg to a stub,
                        # or reverse its direction to reach a new tee.
                        if (new_u-far.u)*(other.u-far.u) <= 0 or abs(new_u-far.u)<.8:
                            continue
                        new_junction=Pt(new_u,junction.v)
                        new_corner=Pt(new_u,other.v)
                        b_new=Segment(new_junction,new_corner,branch.medium,
                                      branch.phase,branch.purpose)
                        p_new=Segment(far,new_corner,prev.medium,prev.phase,prev.purpose)
                        proposed=[s for k,s in enumerate(lines) if k not in (i,j)] + [b_new,p_new]
                        if four_way_nodes(proposed):
                            continue
                        if placements and protected_well_conflicts(proposed,placements):
                            continue
                        if len(forbidden_foreign_contacts(proposed,-1e12)) > len(forbidden_foreign_contacts(lines,-1e12)):
                            continue
                        before=network_fittings(lines)
                        after=network_fittings(proposed)
                        if (after['elbows']>before['elbows'] or after['tees']>before['tees']):
                            continue
                        lines=proposed
                        notes.append((branch.medium,round(junction.u,3),round(new_u,3),round(junction.v,3)))
                        moved=True
                        break
                    if moved:
                        break
                if moved:
                    break
            if moved:
                break
        if not moved:
            break
    return lines,notes
