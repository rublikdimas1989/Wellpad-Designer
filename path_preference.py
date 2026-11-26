"""Deterministic route ordering shared by preview and final routing.

A terminal point (tie-in / fire node) has no directional port.  Therefore
compare the ENTIRE path, including its last connection leg.  For equal
physical length and bend count prefer continuing along U (parallel to the
trunk) before turning toward it, rather than descending next to the node.
This preference is only a tiebreaker; it never licenses a longer pipe or
an extra bend for the same termination point.
"""
from __future__ import annotations

_TOL = 1e-7


def compact_path(points):
    """Remove repeated and intermediate collinear vertices; reject U-turns."""
    result=[]
    for q in points:
        if result and abs(q.u-result[-1].u)<_TOL and abs(q.v-result[-1].v)<_TOL:
            continue
        result.append(q)
        while len(result)>=3:
            a,b,c=result[-3:]
            x1,y1=b.u-a.u,b.v-a.v
            x2,y2=c.u-b.u,c.v-b.v
            if abs(x1*y2-y1*x2)>_TOL:
                break
            if x1*x2+y1*y2 < -_TOL:
                # Backtracking requires a real routing change; don't hide it.
                break
            result.pop(-2)
    return tuple(result)


def path_length(points):
    return sum(abs(b.u-a.u)+abs(b.v-a.v) for a,b in zip(points,points[1:]))


def path_bends(points):
    nodes=compact_path(points)
    return sum(1 for a,b,c in zip(nodes,nodes[1:],nodes[2:])
               if abs((b.u-a.u)*(c.v-b.v)-(b.v-a.v)*(c.u-b.u))>_TOL)


def tie_alignment_key(points, *, tie_at_end=False):
    """Tie node -> target: prefer a long initial U segment, then a late turn.

    A route from the centre to the tie is reversed for comparison.  Source
    direction is used ONLY after the physical length and bend count tie.
    """
    nodes=compact_path(tuple(reversed(points)) if tie_at_end else points)
    if len(nodes)<2:
        return (1,0.,())
    first=nodes[0];second=nodes[1]
    initial_horizontal=abs(second.v-first.v)<_TOL
    straight=abs(second.u-first.u)+abs(second.v-first.v)
    return (0 if initial_horizontal else 1,-round(straight,6),
            tuple((round(q.u,6),round(q.v,6)) for q in nodes))


def tie_path_key(points, *, tie_at_end=False):
    """Independent same-port path: length, physical elbows, route geometry."""
    return (round(path_length(points),6),path_bends(points),
            *tie_alignment_key(points,tie_at_end=tie_at_end))


def tie_network_key(cost,points):
    """Final network: true network cost first, then physical tie route shape."""
    return (round(cost,6),path_bends(points),round(path_length(points),6),
            *tie_alignment_key(points,tie_at_end=True))
