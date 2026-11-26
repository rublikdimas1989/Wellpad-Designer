"""Standard 3 x 1.75 m trunk connecting slabs at external trunk tees.

If a transit tee cannot be shifted to an existing connection slab without
introducing new elbows, it needs its own trunk-width support. A transit
module 1.2 x 3 m must never be used across the main trunk bundle.
"""
from routing import Slab, EPS
from axial_plates import TRUNK_PURPOSES
from fitting_support import slab_distance

PLATE_LENGTH=3.0
PLATE_TYPE='trunk_tap_plate'


def _overlaps(a,b):
    return (min(a.u_end,b.u_end)-max(a.u_start,b.u_start)>1e-6 and
            min(a.v_max,b.v_max)-max(a.v_min,b.v_min)>1e-6)


def create_trunk_tap_plates(segments, slabs, plate, *, strict=False):
    """Make additional conventional trunk-width slabs at off-axis taps.

    Geometry only: nothing is attached through the plate; the physical
    junction remains a three-port tee of the same medium.
    """
    from fitting_support import support_sites
    from routing import Pt
    candidates=[]
    # Реальные тройники и любые пересечения над хребтом требуют широкой
    # хребтовой плиты — не транзитной 1,2 × 3 м. Проверяем уже существующие
    # конструкции до добавления новой.
    for site in support_sites(segments):
        u,v=site.point.u,site.point.v
        medium=next((s.medium for s in segments
                     if s.purpose in TRUNK_PURPOSES and s.horizontal
                     and abs(s.a.v-v)<1e-5
                     and min(s.a.u,s.b.u)-1e-5 <= u <= max(s.a.u,s.b.u)+1e-5),None)
        if medium is None:
            continue
        if any(slab_distance(site.point,slab)<=.25+EPS for slab in slabs):
            continue
        candidates.append((u,v,medium))
    extra=[]
    for u,v,medium in sorted(candidates):
        static=[*slabs,*extra]
        if any(slab_distance(Pt(u,v),slab)<=.25+EPS for slab in static):
            continue
        # For left-center canonical construction trunk exits through
        # plate.u_max, so no new slab may overlap that central plate.
        minimum=plate.trunk_face_u
        right_center=abs(plate.trunk_face_u-plate.u_min)<1e-6
        # Try centered placement, then align with nearest existing edge.
        centers=[u,minimum+PLATE_LENGTH/2]
        for other in static:
            if other.v_min<=v<=other.v_max:
                centers.extend([other.u_end+PLATE_LENGTH/2,
                                other.u_start-PLATE_LENGTH/2])
        possible=[]
        for center in sorted(set(round(x,6) for x in centers),key=lambda q:abs(q-u)):
            lo=center-PLATE_LENGTH/2
            hi=center+PLATE_LENGTH/2
            if (hi>minimum+EPS if right_center else lo<minimum-EPS) or u < lo-.25-EPS or u > hi+.25+EPS:
                continue
            active=[]
            for s in segments:
                if s.purpose not in TRUNK_PURPOSES or not s.horizontal:
                    continue
                left,right=sorted((s.a.u,s.b.u))
                if min(hi,right)-max(lo,left)>EPS and s.medium not in active:
                    active.append(s.medium)
            if medium not in active:continue
            phase='active' if any(s.phase=='active' and s.medium in active and s.purpose in TRUNK_PURPOSES
                                  for s in segments) else 'future'
            new=Slab(lo,hi,plate.v_min,plate.v_max,phase,len(active),tuple(active),PLATE_TYPE)
            if any(_overlaps(new,s) for s in static):
                continue
            possible.append((abs(center-u),new))
        if possible:
            extra.append(min(possible,key=lambda q:q[0])[1])
        elif strict:
            raise ValueError(f'Не помещается соединительная хребтовая плита под тройник {medium} '
                             f'в U={u:.2f}, V={v:.2f}: изменить трассу или положение соседних плит.')
    return extra
