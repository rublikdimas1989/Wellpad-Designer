"""Плиты осевых узлов и вспомогательные плиты хребта/отпаек."""
from __future__ import annotations

from central_plate import PLATE_WIDTH

JUNCTION_PLATE_LENGTH = 3.0
JUNCTION_PLATE_WIDTH = PLATE_WIDTH
JUNCTION_PLATE_TYPE = 'axial_junction'

BRANCH_PLATE_LENGTH = 6.0
BRANCH_PLATE_WIDTH = 1.2
BRANCH_PLATE_TYPE = 'axial_branch'

INTERMEDIATE_SHORT_LENGTH = 3.0
INTERMEDIATE_LONG_LENGTH = 6.0
INTERMEDIATE_WIDTH = PLATE_WIDTH
INTERMEDIATE_PLATE_TYPE = 'trunk_intermediate'

NODE_PLATE_LENGTH = 3.0
NODE_PLATE_WIDTH = 1.2
NODE_PLATE_TYPE = 'terminal_node'
OFFAXIS_PLATE_TYPE = 'offaxis_branch'

TRUNK_PURPOSES = frozenset((
    'Хребет', 'Перспективный хребет', 'Перспективное продление хребта',
))


def _well_branch_segments(segments, well_number: int):
    purpose = f'Отвод от скв. {well_number}'
    return [seg for seg in segments if seg.purpose == purpose]


def _point_eq(a, b, eps=1e-6):
    return abs(a.u-b.u) <= eps and abs(a.v-b.v) <= eps


def _axis_sign(seg, near, far):
    if seg.horizontal:
        return 'h', (1.0 if far.u >= near.u else -1.0), seg.a.v, near.u, far.u
    return 'v', (1.0 if far.v >= near.v else -1.0), seg.a.u, near.v, far.v


def _cluster_by_span(entries, span_limit):
    if not entries:
        return []
    entries=sorted(entries, key=lambda e:e['cross'])
    groups=[]
    current=[]
    for entry in entries:
        if current and entry['cross'] - current[0]['cross'] > span_limit + 1e-6:
            groups.append(current)
            current=[]
        current.append(entry)
    if current:
        groups.append(current)
    return groups


def create_axial_junction_plates(placements, branch_ports, trunk_segments,
                                  lanes, central_plate, media_order):
    """Возвращает (плиты, недостающие концевые отрезки хребта).

    Одна плита на группу присоединений скважины, помещающуюся в 3 м. Если
    точки подключения разнесены шире, создаются несколько независимых плит.
    Их поперечные границы точно совпадают с центральной плитой. Нитки
    хребта, оказавшиеся на плитах, доводятся до её обоих торцов, а не
    обрываются в точке присоединения (вместо ошибочного отвода получается
    тройник с заглушённым продолжением).
    """
    from routing import Slab, Segment, Pt, EPS

    plates=[]
    extensions=[]
    network=list(trunk_segments)
    margin=0.12

    for placement in placements:
        well=placement.well
        if not well.on_axis or well.status=='inactive':
            continue
        joins=[]
        for medium, _direction in placement.connections:
            if medium=='oil_measure':
                continue
            station=branch_ports.get((well.number,medium))
            if station is None:
                raise ValueError(f'Скважина №{well.number}: нет точки подключения {medium} к хребту.')
            candidate=[seg for seg in network
                       if seg.medium==medium and seg.purpose in TRUNK_PURPOSES
                       and seg.horizontal and abs(seg.a.v-lanes[medium])<1e-5
                       and min(seg.a.u,seg.b.u)-1e-5<=station<=max(seg.a.u,seg.b.u)+1e-5]
            if not candidate:
                raise ValueError(f'Скважина №{well.number}: отпайка {medium} '
                                 f'не присоединена к фактической линии хребта.')
            joins.append((float(station), medium))
        if not joins:
            continue
        joins.sort()
        groups=[]
        current=[]
        for item in joins:
            if current and item[0]-current[0][0] > JUNCTION_PLATE_LENGTH-2*margin+EPS:
                groups.append(current)
                current=[]
            current.append(item)
        if current:
            groups.append(current)

        for group in groups:
            min_join,max_join=group[0][0],group[-1][0]
            at_well=placement.point.u
            half=JUNCTION_PLATE_LENGTH/2
            if min_join >= at_well-half+margin-EPS and max_join <= at_well+half-margin+EPS:
                center=at_well
            else:
                center=(min_join+max_join)/2
            low,high=center-half,center+half
            if not (low+EPS < min_join and max_join < high-EPS):
                raise ValueError(f'Скважина №{well.number}: точки подключения не помещаются '
                                 'в границах плиты 3 × 1,75 м.')
            overlap=min(high,central_plate.u_max)-max(low,central_plate.u_min)
            if overlap>EPS:
                raise ValueError(f'Плита отпайков скважины №{well.number} перекрывает '
                                 'центральную плиту. Увеличьте расстояние до центрального узла.')
            for older in plates:
                overlap=min(high,older.u_end)-max(low,older.u_start)
                if overlap>EPS:
                    raise ValueError(f'Плиты отпайков скважины №{well.number} накладываются '
                                     'на соседнюю плиту. Уточните компоновку.')

            used=[]
            phases=[]
            for medium in media_order:
                candidates=[seg for seg in network
                            if seg.medium==medium and seg.purpose in TRUNK_PURPOSES
                            and seg.horizontal and abs(seg.a.v-lanes[medium])<1e-5
                            and min(seg.a.u,seg.b.u)<=high+EPS
                            and max(seg.a.u,seg.b.u)>=low-EPS]
                if not candidates:
                    continue
                if not any(min(high,max(s.a.u,s.b.u)) - max(low,min(s.a.u,s.b.u)) > EPS
                           for s in candidates) and not any(m==medium for _u,m in group):
                    continue
                used.append(medium)
                phases.extend(s.phase for s in candidates)
                left=min(min(s.a.u,s.b.u) for s in candidates)
                right=max(max(s.a.u,s.b.u) for s in candidates)
                if left > low+EPS:
                    nearest=min(candidates,key=lambda s:min(s.a.u,s.b.u))
                    extension=Segment(Pt(low,lanes[medium]),Pt(left,lanes[medium]),
                                      medium,nearest.phase,'Хребет' if nearest.phase=='active'
                                      else 'Перспективный хребет')
                    extensions.append(extension)
                    network.append(extension)
                if right < high-EPS:
                    nearest=max(candidates,key=lambda s:max(s.a.u,s.b.u))
                    extension=Segment(Pt(right,lanes[medium]),Pt(high,lanes[medium]),
                                      medium,nearest.phase,'Хребет' if nearest.phase=='active'
                                      else 'Перспективный хребет')
                    extensions.append(extension)
                    network.append(extension)

            if not used:
                raise ValueError(f'Скважина №{well.number}: на плите отпайков нет ниток хребта.')
            phase='active' if 'active' in phases else 'future'
            plates.append(Slab(low,high,central_plate.v_min,central_plate.v_max,
                               phase,len(used),tuple(used),JUNCTION_PLATE_TYPE))
    return plates,extensions


def create_axial_branch_support_plates(placements, segments, *, plate_offset: float = 1.0):
    """Плиты 1,2 × 6 м под отпаячными линиями осевых скважин.

    Если несколько ниток одной скважины идут единым коридором, ставится ОДНА
    плита по центру коридора, а не отдельная плита под каждую трубу.
    """
    from routing import Slab, EPS

    plates=[]
    for placement in placements:
        well=placement.well
        if not well.on_axis or well.status=='inactive':
            continue
        entries=[]
        for seg in _well_branch_segments(segments, well.number):
            if seg.medium == 'oil_measure':
                continue
            d_a=abs(seg.a.u-placement.point.u)+abs(seg.a.v-placement.point.v)
            d_b=abs(seg.b.u-placement.point.u)+abs(seg.b.v-placement.point.v)
            near, far = (seg.a, seg.b) if d_a <= d_b else (seg.b, seg.a)
            axis, sign, cross, near_along, far_along = _axis_sign(seg, near, far)
            entries.append({'seg':seg,'axis':axis,'sign':sign,'cross':cross,
                            'near':near_along,'far':far_along,'medium':seg.medium})
        for axis in ('h','v'):
            axis_entries=[e for e in entries if e['axis']==axis]
            for sign in (1.0,-1.0):
                corridor=[e for e in axis_entries if e['sign']==sign]
                for group in _cluster_by_span(corridor, BRANCH_PLATE_WIDTH):
                    if not group:
                        continue
                    phase='active' if any(e['seg'].phase=='active' for e in group) else 'future'
                    media=tuple(sorted({e['medium'] for e in group}))
                    cross_min=min(e['cross'] for e in group)
                    cross_max=max(e['cross'] for e in group)
                    cross_center=(cross_min+cross_max)/2
                    near=min(e['near'] for e in group) if sign>0 else max(e['near'] for e in group)
                    far=max(e['far'] for e in group) if sign>0 else min(e['far'] for e in group)
                    start=near + sign*plate_offset
                    end=start + sign*BRANCH_PLATE_LENGTH
                    lo=min(near,far)
                    hi=max(near,far)
                    a=min(start,end); b=max(start,end)
                    if a < lo-EPS or b > hi+EPS:
                        if hi-lo <= BRANCH_PLATE_LENGTH+EPS:
                            a=lo; b=hi
                        else:
                            if sign>0:
                                a=max(lo, min(a, hi-BRANCH_PLATE_LENGTH)); b=a+BRANCH_PLATE_LENGTH
                            else:
                                b=min(hi, max(b, lo+BRANCH_PLATE_LENGTH)); a=b-BRANCH_PLATE_LENGTH
                    if axis=='h':
                        plates.append(Slab(a,b,
                                           cross_center-BRANCH_PLATE_WIDTH/2,
                                           cross_center+BRANCH_PLATE_WIDTH/2,
                                           phase,len(media),media,BRANCH_PLATE_TYPE))
                    else:
                        plates.append(Slab(cross_center-BRANCH_PLATE_WIDTH/2,
                                           cross_center+BRANCH_PLATE_WIDTH/2,
                                           a,b,
                                           phase,len(media),media,BRANCH_PLATE_TYPE))
    return plates


def create_intermediate_trunk_plates(base_plates, trunk_segments, lanes, central_plate):
    """Промежуточные плиты под хребтом между соединительными плитами.

    Между соседними плитами хребта:
      * gap < 5 м  -> дополнительная плита не нужна;
      * 5..11 м    -> ставится плита 1,75 × 3 м по центру;
      * > 11 м     -> ставится плита 1,75 × 6 м по центру.
    """
    from routing import Slab, EPS

    if not base_plates:
        return []
    ordered=sorted(base_plates,key=lambda s:(s.u_start,s.u_end))
    plates=[]
    for left,right in zip(ordered,ordered[1:]):
        gap=right.u_start-left.u_end
        if gap < 5.0-EPS:
            continue
        length = INTERMEDIATE_SHORT_LENGTH if gap <= 11.0+EPS else INTERMEDIATE_LONG_LENGTH
        center=(left.u_end+right.u_start)/2
        u_start=center-length/2
        u_end=center+length/2
        if u_start < left.u_end-EPS or u_end > right.u_start+EPS:
            continue
        used=[]
        phases=[]
        for medium in lanes:
            candidates=[seg for seg in trunk_segments
                        if seg.medium==medium and seg.purpose in TRUNK_PURPOSES and seg.horizontal
                        and abs(seg.a.v-lanes[medium])<1e-5
                        and min(seg.a.u,seg.b.u)<=u_end+1e-6 and max(seg.a.u,seg.b.u)>=u_start-1e-6]
            if not candidates:
                continue
            if not any(min(u_end,max(s.a.u,s.b.u)) - max(u_start,min(s.a.u,s.b.u)) > 1e-6
                       for s in candidates):
                continue
            used.append(medium)
            phases.extend(s.phase for s in candidates)
        if not used:
            continue
        phase='active' if 'active' in phases else 'future'
        plates.append(Slab(u_start,u_end,central_plate.v_min,central_plate.v_max,
                           phase,len(used),tuple(used),INTERMEDIATE_PLATE_TYPE))
    return plates


def _straight_plate_from_point(point, axis, sign, along_limit, phase, media, slab_type,
                               offset, length, width):
    """Полноразмерная плита вдоль линии без обрезки до длины сегмента.

    У исходящей от внеосевой скважины ветки проверка достаточной длины
    выполняется при построении трассы. Здесь размеры не меняем никогда.
    """
    from routing import Slab
    start=(point.u if axis=='h' else point.v) + sign*offset
    end=start+sign*length
    a,b=sorted((start,end))
    if axis=='h':
        return Slab(a,b,point.v-width/2,point.v+width/2,
                    phase,len(media),tuple(sorted(set(media))),slab_type)
    return Slab(point.u-width/2,point.u+width/2,a,b,
                phase,len(media),tuple(sorted(set(media))),slab_type)


def _terminal_plate(node_point, matching_segments, slab_type=NODE_PLATE_TYPE):
    """Полная 1,2 × 3 м плита на выходном узле, с учётом ближайшего отвода.

    Если последнее прямое плечо короче ширины плиты 1,2 м, продольную
    ось плиты совмещаем с предыдущим (перпендикулярным) плечом. Поворот
    и сама конечная точка оказываются внутри границ.
    """
    from routing import Slab
    if not matching_segments:
        return None
    last=next((s for s in matching_segments
               if _point_eq(s.a,node_point) or _point_eq(s.b,node_point)),None)
    if last is None:
        return None
    corner=last.b if _point_eq(last.a,node_point) else last.a
    last_len=abs(corner.u-node_point.u)+abs(corner.v-node_point.v)
    prev=next((s for s in matching_segments
               if s is not last and s.medium==last.medium and s.phase==last.phase
               and s.purpose==last.purpose and s.horizontal!=last.horizontal
               and (_point_eq(s.a,corner) or _point_eq(s.b,corner))),None)
    if prev is not None and last_len < NODE_PLATE_WIDTH-1e-7:
        # Продольная ось вдоль второго от конечного узла участка.
        far=prev.b if _point_eq(prev.a,corner) else prev.a
        margin=0.12
        if prev.horizontal:
            direction=1 if far.u>corner.u else -1
            # Начало у угла с небольшим выпуском за него — вся плита 3 м.
            u1=corner.u-direction*margin
            u2=u1+direction*NODE_PLATE_LENGTH
            v_mid=(corner.v+node_point.v)/2
            return Slab(min(u1,u2),max(u1,u2),
                        v_mid-NODE_PLATE_WIDTH/2,v_mid+NODE_PLATE_WIDTH/2,
                        last.phase,1,(last.medium,),slab_type)
        direction=1 if far.v>corner.v else -1
        v1=corner.v-direction*margin
        v2=v1+direction*NODE_PLATE_LENGTH
        u_mid=(corner.u+node_point.u)/2
        return Slab(u_mid-NODE_PLATE_WIDTH/2,u_mid+NODE_PLATE_WIDTH/2,
                    min(v1,v2),max(v1,v2),last.phase,1,(last.medium,),slab_type)
    if last.horizontal:
        sign=1.0 if corner.u>node_point.u else -1.0
        return _straight_plate_from_point(node_point,'h',sign,corner.u,last.phase,
                                          (last.medium,),slab_type,0,NODE_PLATE_LENGTH,NODE_PLATE_WIDTH)
    sign=1.0 if corner.v>node_point.v else -1.0
    return _straight_plate_from_point(node_point,'v',sign,corner.v,last.phase,
                                      (last.medium,),slab_type,0,NODE_PLATE_LENGTH,NODE_PLATE_WIDTH)


def _cover_parallel_bundle(slab, segments, medium, node_point):
    """Сдвинуть плиту поперёк пучка, сохраняя её фиксированную ширину.

    Учитываются реальные трубы, параллельные оси плиты и пересекающие её
    продольный интервал. Если соседние трубы целиком помещаются в 1,2 м,
    плиту центрируем по их общим наружным габаритам. Не двигаем трубу и
    не растягиваем бетонную плиту.
    """
    from routing import Slab, PIPE_OD
    axis='h' if slab.length>slab.height else 'v'
    along_lo,along_hi=(slab.u_start,slab.u_end) if axis=='h' else (slab.v_min,slab.v_max)
    cross=(node_point.v if axis=='h' else node_point.u)
    radius=PIPE_OD.get(medium,0.1)/2
    included={medium:(cross-radius,cross+radius)}
    nearby=[]
    for segment in segments:
        if (segment.medium==medium or segment.purpose=='Подключение пожарного узла' or
                (axis=='h')!=segment.horizontal):
            continue
        seg_cross=segment.a.v if axis=='h' else segment.a.u
        start,end=(sorted((segment.a.u,segment.b.u)) if axis=='h'
                   else sorted((segment.a.v,segment.b.v)))
        # Должен быть реальный продольный нахлёст с плитой.
        if min(end,along_hi)-max(start,along_lo)<0.3:
            continue
        # Пучок — близкие параллельные нитки, не весь чертёж.
        if abs(seg_cross-cross)>NODE_PLATE_WIDTH:
            continue
        rr=PIPE_OD.get(segment.medium,0.1)/2
        nearby.append((abs(seg_cross-cross),segment.medium,seg_cross,rr))
    for _distance,other_medium,other_cross,rr in sorted(nearby):
        lo=min(v[0] for v in included.values())
        hi=max(v[1] for v in included.values())
        if max(hi,other_cross+rr)-min(lo,other_cross-rr)<=NODE_PLATE_WIDTH+1e-7:
            included[other_medium]=(other_cross-rr,other_cross+rr)
    lo=min(q[0] for q in included.values())
    hi=max(q[1] for q in included.values())
    if len(included)<2:
        return slab
    mid=(lo+hi)/2
    if axis=='h':
        return Slab(slab.u_start,slab.u_end,mid-NODE_PLATE_WIDTH/2,
                    mid+NODE_PLATE_WIDTH/2,slab.phase,len(included),
                    tuple(sorted(included)),slab.slab_type)
    return Slab(mid-NODE_PLATE_WIDTH/2,mid+NODE_PLATE_WIDTH/2,
                slab.v_min,slab.v_max,slab.phase,len(included),
                tuple(sorted(included)),slab.slab_type)


def create_endpoint_support_plates(placements, segments, tie_point, fire_point):
    """Плиты 1,2 × 3 м для точки врезки, пожарного узла и внеосевых скважин."""
    from routing import Slab
    plates=[]
    # Точка врезки.
    tie_segments=[s for s in segments if s.purpose=='Продуктовый нефтепровод: от центра к врезке']
    slab=_terminal_plate(tie_point, tie_segments, NODE_PLATE_TYPE)
    if slab is not None:
        plates.append(_cover_parallel_bundle(slab,segments,'oil_product',tie_point))
    # Пожарный узел.
    fire_segments=[s for s in segments if s.purpose=='Подключение пожарного узла']
    slab=_terminal_plate(fire_point, fire_segments, 'fire_terminal_node')
    if slab is not None:
        # Fire is a separate system even when its medium matches well pipes.
        # Never widen the fire endpoint slab to carry a shared corridor.
        plates.append(slab)
    # Внеосевые скважины: плита 1 м от скважины по направлению исходящей трубы.
    for wp in placements:
        if wp.well.on_axis or wp.well.status=='inactive':
            continue
        import re
        pattern=re.compile(r'скв\.\s*'+str(wp.well.number)+r'(?!\d)',re.IGNORECASE)
        related=[s for s in segments if pattern.search(s.purpose)]
        if not related:
            continue
        # Ищем фактическую точку выхода трубы из скважины: ближайшую к центру скважины.
        endpoints=[]
        for seg in related:
            endpoints.extend((seg.a,seg.b))
        point=min(endpoints, key=lambda q: abs(q.u-wp.point.u)+abs(q.v-wp.point.v))
        touching=[s for s in related if _point_eq(s.a, point) or _point_eq(s.b, point)]
        if not touching:
            continue
        # Выбираем направление самого длинного исходящего участка.
        options=[]
        for seg in touching:
            other=seg.b if _point_eq(seg.a, point) else seg.a
            axis='h' if seg.horizontal else 'v'
            sign=1.0 if ((other.u-point.u) if axis=='h' else (other.v-point.v)) >= 0 else -1.0
            length=abs(other.u-point.u)+abs(other.v-point.v)
            cross=point.v if axis=='h' else point.u
            options.append((length,axis,sign,cross,seg.phase,seg.medium,other))
        length,axis,sign,cross,phase,medium,other=max(options,key=lambda x:x[0])
        # Если участка недостаточно, плиту всё равно прижимаем внутрь имеющегося прямолинейного участка.
        if axis=='h':
            slab=_straight_plate_from_point(point,'h',sign,other.u,phase,(medium,),OFFAXIS_PLATE_TYPE,
                                            1.0,NODE_PLATE_LENGTH,NODE_PLATE_WIDTH)
            # центрируем плиту на фактическом коридоре выхода
            slab=Slab(slab.u_start,slab.u_end,cross-NODE_PLATE_WIDTH/2,cross+NODE_PLATE_WIDTH/2,
                      slab.phase,slab.media_count,slab.media,slab.slab_type)
        else:
            slab=_straight_plate_from_point(point,'v',sign,other.v,phase,(medium,),OFFAXIS_PLATE_TYPE,
                                            1.0,NODE_PLATE_LENGTH,NODE_PLATE_WIDTH)
            slab=Slab(cross-NODE_PLATE_WIDTH/2,cross+NODE_PLATE_WIDTH/2,slab.v_min,slab.v_max,
                      slab.phase,slab.media_count,slab.media,slab.slab_type)
        plates.append(slab)
    return plates


TRANSIT_PLATE_LENGTH = 3.0
TRANSIT_PLATE_WIDTH = 1.2
TRANSIT_PLATE_TYPE = 'transit_run'


def _intervals_overlap(a1,a2,b1,b2,eps=1e-6):
    return min(a2,b2)-max(a1,b1) > eps


def _subtract_intervals(base_interval, blockers):
    start,end=base_interval
    if end-start <= 1e-6:
        return []
    parts=[(start,end)]
    for b0,b1 in sorted((max(start,b0),min(end,b1)) for b0,b1 in blockers if b1-b0>1e-6):
        nxt=[]
        for p0,p1 in parts:
            if b1<=p0+1e-6 or b0>=p1-1e-6:
                nxt.append((p0,p1))
            else:
                if b0>p0+1e-6:
                    nxt.append((p0,b0))
                if b1<p1-1e-6:
                    nxt.append((b1,p1))
        parts=nxt
    return [(a,b) for a,b in parts if b-a>1e-6]


def _is_transit_or_main_segment(seg, offaxis_well_ids=()):
    """Recognize every real external pipe, not just some naming variants.

    Axial branch pipes are handled by their dedicated 1.2 × 6 m modules;
    trunk and central plate pipes have their own dedicated module rules.
    """
    purpose=seg.purpose
    if purpose in TRUNK_PURPOSES or purpose=='Центральная плита: магистральная труба':
        return False
    if purpose.startswith('Отвод от скв. '):
        # The catalog solver uses this exact label for BOTH axial and
        # off-axis wells. Axial takeoffs have their own dedicated modules;
        # off-axis pipe runs need regular 1.2 x 3 m filler plates.
        import re
        match=re.match(r'^Отвод от скв\.\s*(\d+)(?!\d)',purpose)
        return bool(match and int(match.group(1)) in offaxis_well_ids)
    # All other physical segments belong to independent external routing.
    # In particular, do not accidentally omit “К существующей нитке ...”,
    # “К центральному узлу ...”, or future names introduced by the optimizer.
    return True


def _plate_count_and_spacing(length, left_fixed=False, right_fixed=False):
    """(количество, равный зазор, первый отступ) с закреплёнными краями.

    После фиксации угловых плит разбиваем только СВОБОДНЫЕ интервалы.
    Между обычными плитами и закреплёнными краями все промежутки
    строго одинаковы и лежат в 2..4 м. Если геометрически неразрешимо,
    не вставляем заведомо неправильные модули.
    """
    import math
    L=TRANSIT_PLATE_LENGTH
    if length<L-1e-6:
        return None
    for count in range(int((length+1e-6)//L),0,-1):
        remainder=length-count*L
        constrained=count-1+int(left_fixed)+int(right_fixed)
        free_edges=2-int(left_fixed)-int(right_fixed)
        if constrained<=0:
            edge=max(0.0,remainder/2.0)
            if edge<=4.0+1e-6:
                return count,0.0,edge
            continue
        if remainder<2*constrained-1e-6:
            continue
        if free_edges==0:
            gap=remainder/constrained
            if 2.0-1e-6<=gap<=4.0+1e-6:
                return count,gap,gap
        elif free_edges==1:
            gap=max(2.0,min(4.0,remainder/(constrained+.5)))
            if remainder+1e-6>=constrained*gap:
                other_edge=max(0.0,remainder-constrained*gap)
                if other_edge<=4.0+1e-6:
                    return count,gap,gap if left_fixed else other_edge
        else:
            gap=max(2.0,min(4.0,remainder/(constrained+2)))
            edge=(remainder-constrained*gap)/2
            if -1e-6<=edge<=4.0+1e-6:
                return count,gap,max(0.0,edge)

    # Two mandatory supports can leave a gap where the former 2..4 m
    # aesthetic pitch is mathematically impossible (e.g. 6.6 m between
    # two fixed plates: 3 m filler + two 1.8 m clearances). Mandatory
    # fixed supports take priority; do not leave a >4 m unsupported span.
    # In that exceptional case allow a SMALLER clearance, but never a
    # clearance above 4 m or an overlap between plates.
    if length < L-1e-6:
        return None
    fallback=[]
    for count in range(1,int((length+1e-6)//L)+1):
        remainder=length-count*L
        if remainder<-1e-6:continue
        if left_fixed and right_fixed:
            gap=remainder/(count+1)
            edge=gap
            clearance=gap
        elif left_fixed or right_fixed:
            gap=remainder/(count+.5)
            edge=gap if left_fixed else gap/2
            clearance=gap
        else:
            gap=remainder/(count+1)
            edge=gap
            clearance=gap
        if 0<=clearance<=4.0+1e-6 and 0<=edge<=4.0+1e-6:
            # Fewest paid slabs wins; among ties prefer a clearance near 2m.
            fallback.append((count,abs(clearance-2.0),gap,edge))
    if fallback:
        count,_deviation,gap,edge=min(fallback)
        return count,gap,edge
    return None


def _active_bands(segments, axis):
    """Sweep along the pipe direction: a band changes at every start/end.

    Crucially, never assign a pipe to a module outside its true extent.
    The same physical corridor can have 1, 2 and then 3 pipes, and each
    section gets its own centerline and media list.
    """
    from routing import PIPE_OD
    lines=[]
    for seg in segments:
        if not _is_transit_or_main_segment(seg):
            continue
        orient='h' if seg.horizontal else 'v'
        if orient!=axis or seg.length < 1e-6:
            continue
        along=sorted((seg.a.u,seg.b.u) if axis=='h' else (seg.a.v,seg.b.v))
        cross=seg.a.v if axis=='h' else seg.a.u
        lines.append((along[0],along[1],cross,seg.medium,seg.phase))
    if not lines:
        return []

    # Initial clusters by cross-axis proximity. Membership within those
    # clusters is recomputed separately at every longitudinal event.
    lines.sort(key=lambda row:row[2])
    clusters=[]
    for line in lines:
        if clusters and line[2]-clusters[-1][0][2] > TRANSIT_PLATE_WIDTH+1e-6:
            clusters.append([])
        if not clusters:
            clusters.append([])
        clusters[-1].append(line)

    raw=[]
    for cluster in clusters:
        breakpoints=sorted({q for a,b,*_ in cluster for q in (a,b)})
        for start,end in zip(breakpoints,breakpoints[1:]):
            if end-start<=1e-6:
                continue
            midpoint=(start+end)/2
            present={}
            for a,b,cross,medium,phase in cluster:
                if a-1e-6<=midpoint<=b+1e-6:
                    key=(round(cross,6),medium)
                    # Active overrides future when segments overlap.
                    old=present.get(key)
                    if old is None or (old[1]=='future' and phase=='active'):
                        present[key]=(cross,phase)
            if not present:
                continue
            sorted_items=sorted([(c,m,phase) for (rounded,m),(c,phase) in present.items()])
            groups=[]
            for c,m,phase in sorted_items:
                rr=PIPE_OD.get(m,.1)/2
                if groups:
                    proposed_min=min(groups[-1]['lo'],c-rr)
                    proposed_max=max(groups[-1]['hi'],c+rr)
                    if proposed_max-proposed_min <= TRANSIT_PLATE_WIDTH+1e-6:
                        g=groups[-1]
                        g['lo']=proposed_min;g['hi']=proposed_max
                        g['items'].append((round(c,6),m,phase))
                        continue
                groups.append({'lo':c-rr,'hi':c+rr,'items':[(round(c,6),m,phase)]})
            for group in groups:
                signature=tuple(sorted(group['items']))
                raw.append({'axis':axis,'start':start,'end':end,
                            'cross':(group['lo']+group['hi'])/2,
                            'media':tuple(sorted({m for _c,m,_p in group['items']})),
                            'phase':'active' if any(p=='active' for _c,_m,p in group['items']) else 'future',
                            'signature':signature})

    # Coalesce adjacent equal-composition bands. Event boundaries caused by
    # unrelated segments must not arbitrarily restart the plate pitch.
    merged=[]
    for band in sorted(raw,key=lambda b:(b['signature'],b['start'],b['end'])):
        if merged and band['signature']==merged[-1]['signature'] and abs(band['start']-merged[-1]['end'])<=1e-6:
            merged[-1]['end']=band['end']
        else:
            merged.append(dict(band))
    return merged


def _corridor_runs(segments, axis, offaxis_well_ids=()):
    """Continuous spatial corridors, independent of joins/leaves of strands.

    Different media may join halfway through a corridor. This does NOT
    restart the plate series. Each individual slab later gets its width and
    media list from the exact pipes physically crossing that slab.
    """
    from routing import PIPE_OD
    lines=[]
    for seg in segments:
        if not _is_transit_or_main_segment(seg, offaxis_well_ids) or seg.length < 1e-6:
            continue
        if ('h' if seg.horizontal else 'v') != axis:
            continue
        a,b=sorted((seg.a.u,seg.b.u) if axis=='h' else (seg.a.v,seg.b.v))
        cross=seg.a.v if axis=='h' else seg.a.u
        r=PIPE_OD.get(seg.medium,.1)/2
        lines.append((a,b,cross-r,cross+r,cross,seg.medium,seg.phase,
                      seg.purpose=='Подключение пожарного узла'))
    if not lines:
        return []
    lines.sort(key=lambda e:(e[7],e[2],e[3]))
    transverse_clusters=[]
    for line in lines:
        if transverse_clusters:
            cluster=transverse_clusters[-1]
            low=min(x[2] for x in cluster)
            high=max(x[3] for x in cluster)
            if cluster[0][7]==line[7] and max(high,line[3])-min(low,line[2])<=TRANSIT_PLATE_WIDTH+1e-6:
                cluster.append(line)
                continue
        transverse_clusters.append([line])
    runs=[]
    for cluster in transverse_clusters:
        sorted_cluster=sorted(cluster,key=lambda e:(e[0],e[1]))
        active=[];end=None
        for line in sorted_cluster:
            if active and line[0]>end+1e-6:
                runs.append((min(e[0] for e in active),end,active,axis))
                active=[];end=None
            active.append(line)
            end=line[1] if end is None else max(end,line[1])
        if active:
            runs.append((min(e[0] for e in active),end,active,axis))
    return runs


def create_transit_run_plates(segments, existing_slabs, *, offaxis_well_ids=()):
    """Regular transit modules on full route runs, with dynamic bundle centers.

    A line entering/leaving a 2- or 3-pipe bundle changes the center of only
    those modules which physically contain it, not the whole series. The
    regular pitch is continuous across the membership change. Static slabs
    in either orientation reserve their entire rectangular footprint.
    """
    from routing import Slab, EPS
    runs=_corridor_runs(segments,'h',offaxis_well_ids)+_corridor_runs(segments,'v',offaxis_well_ids)
    runs.sort(key=lambda run:(-(run[1]-run[0]),run[3],run[0]))
    modules=[]
    for start,end,lines,axis in runs:
        # Reserve any existing plate touching the maximum cross-section.
        cross_lo=min(e[2] for e in lines)
        cross_hi=max(e[3] for e in lines)
        blockers=[]
        for slab in [*existing_slabs,*modules]:
            slab_cross=(slab.v_min,slab.v_max) if axis=='h' else (slab.u_start,slab.u_end)
            # Conservative full-width corridor intersection, even when the
            # local subset of pipes is narrower at a given station.
            if not _intervals_overlap(cross_lo,cross_hi,*slab_cross):
                continue
            blockers.append((slab.u_start,slab.u_end) if axis=='h' else (slab.v_min,slab.v_max))
        # Запрет установки транзитных плит поверх хребтовых ниток.
        # Прежде регулярно расставленные плиты вертикального подвода
        # могли заходить на горизонтальный хребет даже когда специальные
        # соединительные плиты стоят рядом. Для них резервируем полосу
        # по наружному диаметру магистрали и поперечной ширине плиты.
        for trunk_seg in segments:
            if (trunk_seg.purpose not in TRUNK_PURPOSES or
                not trunk_seg.horizontal or trunk_seg.length<EPS):
                continue
            u0,u1=sorted((trunk_seg.a.u,trunk_seg.b.u))
            t_v=trunk_seg.a.v
            if axis=='v':
                # Прямоугольник будущей плиты шириной 1,2 м пересекает
                # хребтовую нитку; не допускаем вторжения в её створ.
                if cross_hi+TRANSIT_PLATE_WIDTH/2 < u0-EPS or cross_lo-TRANSIT_PLATE_WIDTH/2 > u1+EPS:
                    continue
                blockers.append((t_v-TRANSIT_PLATE_WIDTH/2,t_v+TRANSIT_PLATE_WIDTH/2))
            else:
                if (cross_hi+TRANSIT_PLATE_WIDTH/2 < t_v-EPS or
                    cross_lo-TRANSIT_PLATE_WIDTH/2 > t_v+EPS):
                    continue
                blockers.append((u0,u1))
        for low,high in _subtract_intervals((start,end),blockers):
            left_fixed=any(abs(bhi-low)<=1e-6 for _blo,bhi in blockers)
            right_fixed=any(abs(blo-high)<=1e-6 for blo,_bhi in blockers)
            arrangement=_plate_count_and_spacing(high-low,left_fixed,right_fixed)
            if not arrangement:
                continue
            count,gap,edge=arrangement
            along=low+edge
            for _ in range(count):
                slab_start=along
                slab_end=along+TRANSIT_PLATE_LENGTH
                local=[e for e in lines if _intervals_overlap(e[0],e[1],slab_start,slab_end)]
                if local:
                    lo=min(e[2] for e in local)
                    hi=max(e[3] for e in local)
                    center=(lo+hi)/2
                    cross=(center-TRANSIT_PLATE_WIDTH/2,center+TRANSIT_PLATE_WIDTH/2)
                    medium=tuple(sorted({e[5] for e in local}))
                    phase='active' if any(e[6]=='active' for e in local) else 'future'
                    if axis=='h':
                        slab=Slab(slab_start,slab_end,*cross,phase,len(medium),medium,
                                  'fire_transit_run' if local[0][7] else TRANSIT_PLATE_TYPE)
                    else:
                        slab=Slab(*cross,slab_start,slab_end,phase,len(medium),medium,
                                  'fire_transit_run' if local[0][7] else TRANSIT_PLATE_TYPE)
                    # Defensive geometric check: transitions can move the
                    # center even though the fixed-max-width blocker did not.
                    if not any(_intervals_overlap(slab.u_start,slab.u_end,o.u_start,o.u_end)
                               and _intervals_overlap(slab.v_min,slab.v_max,o.v_min,o.v_max)
                               for o in [*existing_slabs,*modules]):
                        modules.append(slab)
                along+=TRANSIT_PLATE_LENGTH+gap
    return modules


def offaxis_unsupported_gaps(segments, slabs, offaxis_well_ids=(), max_clear=4.0):
    """Find off-axis straight pipe spans left without a support for >4 m.

    A plate counts only where its rectangle fully contains the pipe OD in
    the transverse direction. Checking the actual slab geometry (not just
    counts) prevents understating costs when the filler generator skips an
    interval between two mandatory supports.
    """
    import re
    from routing import PIPE_OD
    offaxis=set(offaxis_well_ids)
    missing=[]
    for seg in segments:
        match=re.match(r'^Отвод от скв\.\s*(\d+)(?!\d)',seg.purpose)
        if not match or int(match.group(1)) not in offaxis or seg.length<=max_clear+1e-6:
            continue
        horizontal=seg.horizontal
        cross=seg.a.v if horizontal else seg.a.u
        low,high=sorted((seg.a.u,seg.b.u) if horizontal else (seg.a.v,seg.b.v))
        half=PIPE_OD.get(seg.medium,.1)/2
        covered=[]
        for slab in slabs:
            c0,c1=(slab.v_min,slab.v_max) if horizontal else (slab.u_start,slab.u_end)
            if c0>cross-half+1e-6 or c1<cross+half-1e-6:
                continue
            b0,b1=(slab.u_start,slab.u_end) if horizontal else (slab.v_min,slab.v_max)
            a,b=max(low,b0),min(high,b1)
            if b-a>1e-6:
                covered.append((a,b))
        pos=low
        for a,b in sorted(covered):
            if a-pos>max_clear+1e-6:
                missing.append((int(match.group(1)),seg.medium,round(pos,3),round(a,3),round(a-pos,3)))
            pos=max(pos,b)
        if high-pos>max_clear+1e-6:
            missing.append((int(match.group(1)),seg.medium,round(pos,3),round(high,3),round(high-pos,3)))
    return missing


def fire_shared_support_violations(segments, slabs, *, min_shared_length=0.3):
    """Reject a paid slab if it supports positive runs of fire AND other externals.

    Structural trunk and central plate connections are not bundles. An
    orthogonal crossing at a point also does not count as a shared run.
    """
    fire=[s for s in segments if s.purpose=='Подключение пожарного узла']
    other=[s for s in segments if s.purpose not in TRUNK_PURPOSES and
           s.purpose not in ('Центральная плита: магистральная труба',
                             'Подключение пожарного узла')]
    def overlap_len(seg,slab):
        if seg.horizontal:
            if not slab.v_min-1e-6 <= seg.a.v <= slab.v_max+1e-6:return 0.
            return max(0.,min(slab.u_end,max(seg.a.u,seg.b.u))-
                             max(slab.u_start,min(seg.a.u,seg.b.u)))
        if not slab.u_start-1e-6 <= seg.a.u <= slab.u_end+1e-6:return 0.
        return max(0.,min(slab.v_max,max(seg.a.v,seg.b.v))-
                         max(slab.v_min,min(seg.a.v,seg.b.v)))
    structural={'central','axial_junction','trunk_intermediate','trunk_tap_plate'}
    return [slab for slab in slabs if slab.slab_type not in structural
            if any(overlap_len(a,slab)>min_shared_length and
                   any(a.horizontal==b.horizontal and
                       overlap_len(b,slab)>min_shared_length for b in other)
                   for a in fire)]


def fire_unsupported_gaps(segments, slabs, max_clear=4.0):
    """A fire run must have its OWN row of support slabs, not borrowed slabs.

    Fire-to-trunk attachment itself may use a structural connection plate,
    but that does not count as a shared longitudinal bundle.  Only fire
    terminal, fire filler and a non-shared fitting support contribute.
    """
    from routing import PIPE_OD
    fire=[s for s in segments if s.purpose=='Подключение пожарного узла']
    others=[s for s in segments if s.purpose not in TRUNK_PURPOSES and
            s.purpose not in ('Центральная плита: магистральная труба',
                              'Подключение пожарного узла')]
    missing=[]
    for seg in fire:
        if seg.length<=max_clear+1e-6:continue
        horizontal=seg.horizontal
        cross=seg.a.v if horizontal else seg.a.u
        lo,hi=sorted((seg.a.u,seg.b.u) if horizontal else (seg.a.v,seg.b.v))
        half=PIPE_OD.get(seg.medium,.1)/2
        intervals=[]
        for slab in slabs:
            if slab.slab_type not in ('fire_terminal_node','fire_transit_run','fire_boundary_fixed','fitting_support'):
                continue
            if slab.slab_type=='fitting_support' and any(
                other.horizontal==horizontal and
                abs((other.a.v if horizontal else other.a.u)-cross)<1.2 and
                min((slab.u_end if horizontal else slab.v_max),
                    max(other.a.u,other.b.u) if horizontal else max(other.a.v,other.b.v))-
                max((slab.u_start if horizontal else slab.v_min),
                    min(other.a.u,other.b.u) if horizontal else min(other.a.v,other.b.v))>.3
                for other in others):
                continue
            a,b=(slab.v_min,slab.v_max) if horizontal else (slab.u_start,slab.u_end)
            if a>cross-half+1e-6 or b<cross+half-1e-6:continue
            s0,s1=(slab.u_start,slab.u_end) if horizontal else (slab.v_min,slab.v_max)
            a,b=max(lo,s0),min(hi,s1)
            if b-a>1e-6:intervals.append((a,b))
        pos=lo
        for a,b in sorted(intervals):
            if a-pos>max_clear+1e-6:
                missing.append((seg.medium,round(pos,2),round(a,2),round(a-pos,2)))
            pos=max(pos,b)
        if hi-pos>max_clear+1e-6:
            missing.append((seg.medium,round(pos,2),round(hi,2),round(hi-pos,2)))
    return missing
