"""v88: Physical transverse packing of independent transit pipes.

The slab is 1.2 m wide, and 0.30 m is the AXIS spacing between neighboring
transit pipes. The whole pack must be centered by the slab generator; this
module validates only the final physical packing.
"""
from __future__ import annotations
from routing import PIPE_OD

AXIS_GAP = 0.30
TRANSIT_WIDTH = 1.20


def profile(axes):
    """axes: iterable of (coordinate, medium) for parallel distinct pipes.

    Returns (physical_span, axis_gaps, media_in_order).
    """
    distinct={(round(float(x),6),str(m)):(float(x),str(m),PIPE_OD[m]/2) for x,m in axes}
    lines=sorted(distinct.values())
    if not lines:
        return (0.0,(),())
    left=min(x-r for x,m,r in lines)
    right=max(x+r for x,m,r in lines)
    gaps=tuple((b[0]-a[0]) for a,b in zip(lines,lines[1:]))
    return right-left,gaps,tuple(m for x,m,r in lines)


def fits(axes, slab_width=TRANSIT_WIDTH, gap=AXIS_GAP):
    span,gaps,_=profile(axes)
    return span<=slab_width+1e-6 and all(g>=gap-1e-6 for g in gaps)


def plate_bundle_issues(segments,slabs,*,minimum=AXIS_GAP):
    """Validate long, parallel pipes actually supported by a small slab.

    Only independent straight longitudinal lengths >1 m count. Orthogonal
    segments of bends are not false parallel bundle members.
    """
    errors=[]
    allowed={'transit_run','boundary_fixed','terminal_node','offaxis_branch',
             'fitting_support'}
    for slab in slabs:
        if slab.slab_type not in allowed:continue
        horizontal=slab.length>=slab.height
        along0,along1=((slab.u_start,slab.u_end) if horizontal else (slab.v_min,slab.v_max))
        cross0,cross1=((slab.v_min,slab.v_max) if horizontal else (slab.u_start,slab.u_end))
        axes={}
        for s in segments:
            if s.horizontal!=horizontal or s.length<1.0:continue
            c=s.a.v if horizontal else s.a.u
            r=PIPE_OD[s.medium]/2
            lo,hi=(sorted((s.a.u,s.b.u)) if horizontal else sorted((s.a.v,s.b.v)))
            if min(hi,along1)-max(lo,along0)<1.0:continue
            if cross0-1e-6<=c-r and c+r<=cross1+1e-6:
                axes[(round(c,5),s.medium)]=(c,s.medium)
        if len(axes)<2:continue
        span,gaps,order=profile(axes.values())
        # gaps are AXIS-to-AXIS distances; user rule is ≥ 0.30 m on a 1.2 m slab.
        if span>min(slab.length,slab.height)+1e-5 or any(g<minimum-1e-5 for g in gaps):
            errors.append((slab.slab_type,tuple(sorted((round(v[0],4),v[1]) for v in axes.values())),
                           round(span,4),tuple(round(x,4) for x in gaps)))
    return errors
