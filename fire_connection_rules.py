"""Независимый пожарный вывод: только свободный торец ЦП либо дальний торец хребта.

Пожарный узел — координатная точка без сторон/ориентации. Алгоритм строит
продолжение торцевой магистрали (фантомную ось U), затем поворачивает к точке.
Никаких пучков с пожарным выводом и дополнительных боковых портов.
"""
from dataclasses import dataclass
from routing import Pt, EPS

TRUNK_PURPOSES = frozenset(('Хребет', 'Перспективный хребет', 'Перспективное продление хребта'))

@dataclass(frozen=True)
class FireEnd:
    name: str
    point: Pt
    direction: int  # по U, только наружу от занятого торца
    kind: str       # plate или trunk


def fire_ends(plate, lanes, segments, medium):
    """Оба допустимых начала, если физическая хребтовая нитка существует.

    Противоположный от плиты торец определяется по ФАКТИЧЕСКИМ отрезкам
    хребта для заданной среды, а не по общему концу всех остальных труб.
    """
    plate_pt=plate.port(medium,lanes,'outer')
    plate_dir=1 if plate.outer_face_u > plate.trunk_face_u else -1
    ends=[FireEnd('Наружный торец центральной плиты',plate_pt,plate_dir,'plate')]
    trunk_dir=-plate_dir
    pieces=[s for s in segments if s.medium==medium and s.purpose in TRUNK_PURPOSES and s.horizontal]
    if pieces:
        far=(max(max(s.a.u,s.b.u) for s in pieces) if trunk_dir>0
             else min(min(s.a.u,s.b.u) for s in pieces))
        # Отсекаем вырожденную или расположенную внутри самой плиты нитку.
        if (far-plate.trunk_face_u)*trunk_dir>EPS:
            ends.append(FireEnd('Дальний свободный торец хребта',Pt(far,lanes[medium]),trunk_dir,'trunk'))
    return tuple(ends)


def _clean(points):
    result=[]
    for point in points:
        if result and abs(point.u-result[-1].u)<EPS and abs(point.v-result[-1].v)<EPS:
            continue
        if len(result)>=2:
            a,b=result[-2:]
            if ((abs(a.u-b.u)<EPS and abs(b.u-point.u)<EPS and
                 (b.v-a.v)*(point.v-b.v)>=-EPS) or
                (abs(a.v-b.v)<EPS and abs(b.v-point.v)<EPS and
                 (b.u-a.u)*(point.u-b.u)>=-EPS)):
                result[-1]=point
                continue
        result.append(point)
    return tuple(result)


def fire_candidate_paths(endpoint:FireEnd, fire:Pt):
    """Приоритет: торец -> проекция пожарного узла на фантомную ось -> узел.

    Дополнительные варианты обхода также начинают движение строго вдоль
    фантомной оси НАРУЖУ от торца. Они нужны, когда кратчайший L-образный
    путь блокируется скважиной или независимыми трубами.
    """
    p=endpoint.point
    sign=endpoint.direction
    forward=(fire.u-p.u)*sign
    raw=[]
    if forward>EPS:
        raw.append((p,Pt(fire.u,p.v),fire))
        # Равные по длине альтернативы поворота на той же фантомной оси
        # допускаются только после предпочтительного створа проекции.
        for fraction in (.25,.5,.75):
            via=p.u+(fire.u-p.u)*fraction
            raw.append((p,Pt(via,p.v),Pt(via,fire.v),fire))
    # Если проекция позади торца или нужна иная трасса, прокладываем
    # короткое наружное продолжение, далее поворот и обход.
    for advance in (1.5,3.,6.,10.,15.,25.):
        via=p.u+sign*advance
        raw.append((p,Pt(via,p.v),Pt(via,fire.v),fire))
    found=[];seen=set()
    for nodes in raw:
        route=_clean(nodes)
        if len(route)<2 or route in seen:continue
        if any(abs(a.u-b.u)>EPS and abs(a.v-b.v)>EPS for a,b in zip(route,route[1:])):
            continue
        if abs(route[1].v-p.v)>EPS or (route[1].u-p.u)*sign<=EPS:
            continue
        seen.add(route);found.append(route)
    return tuple(found)


def route_length(route):
    return sum(abs(b.u-a.u)+abs(b.v-a.v) for a,b in zip(route,route[1:]))


def route_bends(route):
    return sum(1 for a,b,c in zip(route,route[1:],route[2:])
               if (abs(b.u-a.u)>EPS and abs(c.v-b.v)>EPS) or
                  (abs(b.v-a.v)>EPS and abs(c.u-b.u)>EPS))
