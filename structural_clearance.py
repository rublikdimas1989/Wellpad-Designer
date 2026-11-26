"""v82: fixed 1.5 m safety corridors around trunk and central slab.

Dimensions are in local U/V metres. The distance is measured from the
physical rectangle/outer pipe surface to the NEAREST EDGE of a 1.2x3 m
transit module, not its centre. Only a final, direct incoming segment may
enter a reserved corridor to join its matching physical infrastructure.
All other route geometry is catalogued, never synthesized here.
"""
from __future__ import annotations
from dataclasses import dataclass

CLEARANCE = 1.5
PLATE_LENGTH = 3.0
PLATE_WIDTH = 1.2
EPS = 1e-5
TRUNK = frozenset(('Хребет','Перспективный хребет',
                   'Перспективное продление хребта'))


@dataclass(frozen=True)
class Buffer:
    kind: str
    x0: float
    x1: float
    y0: float
    y1: float
    # Physical rectangle BEFORE expansion by the exclusion distance.

    def expanded(self):
        return self.x0-CLEARANCE, self.x1+CLEARANCE, self.y0-CLEARANCE, self.y1+CLEARANCE


def infrastructure_buffers(survey, plate):
    """One trunk-band box and one slab box, in real local metre coordinates."""
    from routing import PIPE_OD
    segments=[s for s in survey.trunk_segments if s.purpose in TRUNK]
    result=[]
    if segments:
        # Pipe outside diameters: the free strip is outside the whole group.
        result.append(Buffer('trunk',min(min(s.a.u,s.b.u) for s in segments),
            max(max(s.a.u,s.b.u) for s in segments),
            min(s.a.v-PIPE_OD[s.medium]/2 for s in segments),
            max(s.a.v+PIPE_OD[s.medium]/2 for s in segments)))
    result.append(Buffer('central',plate.u_min,plate.u_max,plate.v_min,plate.v_max))
    return tuple(result)


def _intersection_length(a,b,rect):
    """Positive overlap of orthogonal segment with a CLOSED rectangle."""
    x0,x1,y0,y1=rect
    if abs(a.v-b.v)<EPS:
        if a.v < y0-EPS or a.v > y1+EPS:return 0.
        return max(0.,min(max(a.u,b.u),x1)-max(min(a.u,b.u),x0))
    if abs(a.u-b.u)<EPS:
        if a.u < x0-EPS or a.u > x1+EPS:return 0.
        return max(0.,min(max(a.v,b.v),y1)-max(min(a.v,b.v),y0))
    return 0.


def _attach_kind(q):
    if q.target.startswith('virtual:plate:'):return 'central'
    if q.target.startswith('base:trunk:'):return 'trunk'
    return None


def _terminal_run_start(q):
    """First node of the last uninterrupted straight run (gate nodes included)."""
    if len(q.path)<2:return 0
    a,b=q.path[-2:]
    horizontal=abs(a.v-b.v)<EPS
    i=len(q.path)-2
    while i>0:
        prev=q.path[i-1];now=q.path[i]
        if horizontal:
            if abs(prev.v-now.v)>=EPS or abs(now.v-b.v)>=EPS:break
            if (now.u-prev.u)*(b.u-now.u)<-EPS:break
        else:
            if abs(prev.u-now.u)>=EPS or abs(now.u-b.u)>=EPS:break
            if (now.v-prev.v)*(b.v-now.v)<-EPS:break
        i-=1
    return i


def _valid_terminal_approach(q,buffer):
    """Only the LAST entire straight segment may enter its target corridor."""
    if len(q.path)<2 or _attach_kind(q)!=buffer.kind:return False
    a,b=q.path[_terminal_run_start(q)],q.path[-1]
    if buffer.kind=='trunk':
        # Side joining is transverse to the predominantly U-oriented trunk;
        # far-end joining is parallel to U, only beyond an actual end.
        transverse=(abs(a.u-b.u)<EPS and buffer.x0-EPS<=b.u<=buffer.x1+EPS)
        end=(abs(a.v-b.v)<EPS and
             (abs(b.u-buffer.x0)<EPS or abs(b.u-buffer.x1)<EPS))
        return transverse or end
    # The central plate sides are at V min/max; its free fire-facing end
    # is at either U min/max. The allowed target port is independently
    # checked by the Connection catalogue.
    side=(abs(a.u-b.u)<EPS and buffer.x0-EPS<=b.u<=buffer.x1+EPS)
    end=(abs(a.v-b.v)<EPS and
         (abs(b.u-buffer.x0)<EPS or abs(b.u-buffer.x1)<EPS))
    return side or end


def clearance_violations(q,survey,plate):
    """Returns exact reasons; a bad path remains visible as *invalid* in Связь."""
    if not q.path or q.source.startswith('well:') and any(
        w.well.number==int(q.source.split(':')[1]) and w.well.on_axis
        for w in survey.wells):
        return ()
    errors=[]
    for buffer in infrastructure_buffers(survey,plate):
        expanded=buffer.expanded()
        for i,(a,b) in enumerate(zip(q.path,q.path[1:])):
            if _intersection_length(a,b,expanded)<=EPS:continue
            if i>=_terminal_run_start(q) and _valid_terminal_approach(q,buffer):
                continue
            errors.append('Транзит внутри защитной полосы 1,5 м: '+
                          ('хребет' if buffer.kind=='trunk' else 'центральная плита'))
            break
    return tuple(errors)


def _inside_fixed(slab,point):
    return slab.u_start-EPS <= point.u <= slab.u_end+EPS and slab.v_min-EPS <= point.v <= slab.v_max+EPS


def _slab_overlap(a,b):
    return min(a.u_end,b.u_end)-max(a.u_start,b.u_start)>EPS and min(a.v_max,b.v_max)-max(a.v_min,b.v_min)>EPS


def boundary_supports(survey,plate,selected,existing):
    """Endmost 1.2x3 m fixed supports, with their near edge 1.5 m away.

    The candidate is never moved: if the last incoming straight run cannot
    take a 3m module outside the keep-out corridor, reject the combination.
    Two pipes may share this slab ONLY if their physical transverse spread
    including OD fits within the 1.2m width. Fire has its own group.
    """
    from routing import Slab,PIPE_OD
    buffers={b.kind:b for b in infrastructure_buffers(survey,plate)}
    proposed=[]
    for q in selected:
        kind=_attach_kind(q)
        if not kind or q.source.startswith('well:') and any(
            wp.well.number==int(q.source.split(':')[1]) and wp.well.on_axis
            for wp in survey.wells):continue
        if len(q.path)<2:continue
        a,b=q.path[_terminal_run_start(q)],q.path[-1]
        buffer=buffers[kind]
        # The terminal straight line must actually intersect the physical
        # infrastructure; a merely nearby but unrelated run is not an anchor.
        touches_end_face=(abs(a.v-b.v)<EPS and
                         (abs(b.u-buffer.x0)<EPS or abs(b.u-buffer.x1)<EPS) and
                         buffer.y0-EPS<=b.v<=buffer.y1+EPS)
        if (_intersection_length(a,b,(buffer.x0,buffer.x1,buffer.y0,buffer.y1))<=EPS
            and not touches_end_face):
            continue
        if abs(a.u-b.u)<EPS:
            axis='v'; direction=1 if a.v>b.v else -1
            if not (a.v>buffer.y1+EPS or a.v<buffer.y0-EPS):
                continue
            border=(buffer.y1 if direction==1 else buffer.y0)
            near=border+direction*CLEARANCE
            far=near+direction*PLATE_LENGTH
            if min(a.v,b.v)>min(near,far)+EPS or max(a.v,b.v)<max(near,far)-EPS:
                # There is no complete 3 m transit support within this route.
                raise ValueError(f'{q.source} / {q.medium}: недостаточно прямого участка '+
                    'для фиксированной плиты за защитной полосой 1,5 м')
            cross=b.u; span=(min(near,far),max(near,far));center=cross
        elif abs(a.v-b.v)<EPS:
            axis='h'; direction=1 if a.u>b.u else -1
            if not (a.u>buffer.x1+EPS or a.u<buffer.x0-EPS):continue
            border=buffer.x1 if direction==1 else buffer.x0
            near=border+direction*CLEARANCE
            far=near+direction*PLATE_LENGTH
            if min(a.u,b.u)>min(near,far)+EPS or max(a.u,b.u)<max(near,far)-EPS:
                raise ValueError(f'{q.source} / {q.medium}: недостаточно прямого участка '+
                    'для крайней фиксированной плиты за полосой 1,5 м')
            cross=b.v;span=(min(near,far),max(near,far));center=cross
        else:
            continue
        phase='future' if q.source.startswith('well:') and any(
            wp.well.number==int(q.source.split(':')[1]) and wp.well.status=='future'
            for wp in survey.wells) else 'active'
        proposed.append((axis,span,cross,PIPE_OD[q.medium]/2,q.medium,phase,q.source=='fire',q.source))

    # Group parallel runs with the same prescribed longitudinal interval.
    # Fire can NEVER borrow the other lines' transverse support.
    groups={}
    for item in proposed:
        axis,span,cross,r,medium,phase,fire,source=item
        key=(axis,tuple(round(x,5) for x in span),fire)
        groups.setdefault(key,[]).append(item)
    slabs=[]
    for (axis,span,fire),group in groups.items():
        pending=sorted(group,key=lambda v:v[2]); clusters=[]
        for item in pending:
            if clusters:
                lo=min(e[2]-e[3] for e in clusters[-1]);hi=max(e[2]+e[3] for e in clusters[-1]);
                if max(hi,item[2]+item[3])-min(lo,item[2]-item[3])<=PLATE_WIDTH+EPS:
                    clusters[-1].append(item);continue
            clusters.append([item])
        for cluster in clusters:
            low=min(v[2]-v[3] for v in cluster)
            high=max(v[2]+v[3] for v in cluster)
            centre=(low+high)/2
            media=tuple(sorted(set(v[4] for v in cluster)))
            phase='active' if any(v[5]=='active' for v in cluster) else 'future'
            if axis=='v':
                slab=Slab(centre-PLATE_WIDTH/2,centre+PLATE_WIDTH/2,
                          *span,phase,len(media),media,
                          'fire_boundary_fixed' if fire else 'boundary_fixed')
            else:
                slab=Slab(*span,centre-PLATE_WIDTH/2,centre+PLATE_WIDTH/2,
                          phase,len(media),media,
                          'fire_boundary_fixed' if fire else 'boundary_fixed')
            # Allow an already fixed slab satisfying this exact edge location,
            # but do not claim overlapping slabs can occupy the same concrete.
            if any(_slab_overlap(slab,old) for old in [*existing,*slabs]):
                raise ValueError('Крайняя фиксированная плита в 1,5 м конфликтует '+
                                  'с ранее установленной опорной плитой')
            slabs.append(slab)
    return slabs


def support_clearance_violations(slabs,survey,plate):
    """No ordinary support can occupy the reserved strip.

    Structural (trunk, axial takeoff and central) slabs are the supported
    infrastructure itself, not third-party transit foundations.
    """
    structural={'central','axial_junction','axial_branch',
                'trunk_intermediate','trunk_tap_plate'}
    errors=[]
    for slab in slabs:
        if slab.slab_type in structural:continue
        for zone in infrastructure_buffers(survey,plate):
            x0,x1,y0,y1=zone.expanded()
            if (min(slab.u_end,x1)-max(slab.u_start,x0)>EPS and
                min(slab.v_max,y1)-max(slab.v_min,y0)>EPS):
                errors.append((slab.slab_type,zone.kind))
    return errors
