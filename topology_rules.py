"""Topology of physical pipe connections: a tee has at most three ports.

Different media may cross in plan; this module only considers connections
of the *same* medium. It deliberately does not interpret a plain drawing
crossing as a fitting unless it is an endpoint on the network graph.
"""
from collections import defaultdict
from dataclasses import replace


def four_way_nodes(segments):
    from pipe_render import make_network
    result=[]
    for medium,edges in make_network(segments).items():
        graph=defaultdict(set)
        for a,b,_ in edges:
            graph[a].add(b);graph[b].add(a)
        for pt,nbrs in graph.items():
            if len(nbrs)>3:
                result.append((medium,pt,len(nbrs)))
    return sorted(result)


def _point_match(p,xy):
    return abs(p.u-xy[0])<1e-5 and abs(p.v-xy[1])<1e-5


def relocate_fourway_branches(segments, placements=(), *, min_shift=0.65):
    """При 4-стороннем узле перенести боковой подвод к новому тройнику.

    Старая нитка без изменения направления остаётся магистралью; от конца
    одной подводимой ветки строится локальная ступень к ближайшему
    свободному участку этой магистрали. Узлы на пожарной линии и внутри
    центральной плиты не меняются автоматически.
    """
    from routing import (Pt,Segment,protected_well_conflicts,
                         forbidden_foreign_contacts,collision_notes)

    current=list(segments)
    changes=[]
    for _ in range(12):
        bad=four_way_nodes(current)
        if not bad:
            return current,changes
        medium,xy,_degree=bad[0]
        graph_xy=Pt(*xy)
        # Сначала переносим внешнее подключение, не трогая осевые отпайки,
        # хребет и пожарную линию.
        movable=[]
        for i,s in enumerate(current):
            if s.medium!=medium or s.length<1.6:
                continue
            if not (_point_match(s.a,xy) or _point_match(s.b,xy)):
                continue
            purpose=s.purpose.casefold()
            if ('пожарного узла' in purpose or 'хребет' in purpose or
                'плита' in purpose or 'отвод от скв.' in purpose):
                continue
            far=s.b if _point_match(s.a,xy) else s.a
            movable.append((i,s,far))
        # Если такой ветки нет, выдаём явную ошибку, не рисуем четверник.
        best=None
        original_foreign=len(forbidden_foreign_contacts(current,-1e12))
        original_overlaps=sum('Наложение осей' in note for note in collision_notes(current))
        for i,s,far in movable:
            for side in (-1,1):
                for shift in (min_shift,1.0,1.5,2.0):
                    # Сдвигаемся по оси, перпендикулярной подводимой ветке:
                    # так новая точка остаётся на старой магистрали.
                    moved=(Pt(xy[0]+side*shift,xy[1]) if not s.horizontal else
                           Pt(xy[0],xy[1]+side*shift))
                    # Нужна реальная магистраль того же типа на новой точке.
                    base=next((b for b in current if b is not s and b.medium==medium and
                               ((b.horizontal and abs(b.a.v-moved.v)<1e-5 and
                                 min(b.a.u,b.b.u)+0.05<=moved.u<=max(b.a.u,b.b.u)-0.05) or
                                (not b.horizontal and abs(b.a.u-moved.u)<1e-5 and
                                 min(b.a.v,b.b.v)+0.05<=moved.v<=max(b.a.v,b.b.v)-0.05))),None)
                    if base is None:
                        continue
                    # Уходим с исходной ветки раньше узла; нельзя снова
                    # пересечь старый четверник на последнем участке.
                    retreat=min(1.0,s.length*0.35)
                    if s.horizontal:
                        heading=1 if far.u>xy[0] else -1
                        turn=Pt(xy[0]+heading*retreat,xy[1])
                        corner=Pt(turn.u,moved.v)
                    else:
                        heading=1 if far.v>xy[1] else -1
                        turn=Pt(xy[0],xy[1]+heading*retreat)
                        corner=Pt(moved.u,turn.v)
                    replacement=[Segment(far,turn,s.medium,s.phase,s.purpose),
                                 Segment(turn,corner,s.medium,s.phase,s.purpose),
                                 Segment(corner,moved,s.medium,s.phase,s.purpose)]
                    proposed=[x for j,x in enumerate(current) if j!=i]+replacement
                    if len(four_way_nodes(proposed)) >= len(bad):
                        continue
                    # Не добавлять трубу в 5-метровую зону чужой скважины.
                    if placements and protected_well_conflicts(proposed,placements):
                        continue
                    if len(forbidden_foreign_contacts(proposed,-1e12)) > original_foreign:
                        continue
                    if sum('Наложение осей' in note for note in collision_notes(proposed)) > original_overlaps:
                        continue
                    score=sum(x.length for x in replacement)-s.length
                    if best is None or (score,shift)<best[0]:
                        best=((score,shift),proposed,(medium,xy,(moved.u,moved.v)))
        if best is None:
            raise ValueError(f'Недопустимый четверник на {medium} в точке '
                             f'({xy[0]:.2f}; {xy[1]:.2f}). Нет места для '
                             'переноса подводимой ветки на отдельный тройник.')
        _,current,detail=best
        changes.append(detail)
    raise ValueError('Не удалось устранить все четырёхсторонние соединения; '
                     'нужно изменить расположение коммуникаций.')


def external_trunk_gate(point, trunk_side, *, clearance=0.65):
    """Gate outside the trunk relative to NDS, for a lateral (not end) tap.

    Axial well takeoffs are a separate, fixed exception: those originate on
    the NDS side of the trunk. This function is for all *external* branches.
    """
    from routing import Pt
    if trunk_side not in (-1, 1):
        raise ValueError('Некорректная сторона хребта')
    return Pt(point.u, point.v + trunk_side * clearance)


def external_trunk_approach(path, trunk_v, trunk_side):
    """Last nonzero segment must approach perpendicularly from outside."""
    if len(path)<2 or trunk_side not in (-1,1):
        return False
    endpoint=path[-1]
    if abs(endpoint.v-trunk_v)>1e-5:
        return False
    for prior in reversed(path[:-1]):
        if abs(prior.u-endpoint.u)+abs(prior.v-endpoint.v)>1e-7:
            return (abs(prior.u-endpoint.u)<1e-5
                    and (prior.v-endpoint.v)*trunk_side>1e-5)
    return False
