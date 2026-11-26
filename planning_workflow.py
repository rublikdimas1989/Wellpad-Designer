"""Поэтапное проектирование: исходные элементы, независимые связи и выбор маршрутов.

Связь: только геометрические кандидаты; магистрали на этом этапе виртуальны.
Все точки на непрерывных объектах нельзя перечислить конечным списком.
Точный глобальный оптимум для общего проекта данным модулем не заявляется.
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from itertools import product
from math import hypot
from hashlib import sha1
from central_plate import make_plate, PlateGeometry
from routing import (Frame, Pt, Project, Result, WellPlacement, axial_u,
                     well_connections, MEDIA, PIPE_OD, Segment, WELL_GUARD_RADIUS,
                     well_medium_outlets, well_connection_normal, WELL_MIN_STRAIGHT,
                     OFFAXIS_MIN_STRAIGHT, line_phase,
                     protected_line_ok, lane_positions, rectilinear_safe_path,
                     base_axial_trunk_reaches, axial_u, trunk_inner_zone,
                     inner_trunk_parallel)

DIRS = ('u+', 'u-', 'v+', 'v-')
DIRECTION = {'u+':(1.,0.),'u-':(-1.,0.),'v+':(0.,1.),'v-':(0.,-1.)}

@dataclass(frozen=True)
class Element:
    key: str
    label: str
    center: Pt
    media: tuple[str,...]
    kind: str
    well_number: int|None = None
    status: str = 'active'

@dataclass(frozen=True)
class LinkCandidate:
    source: str
    target: str
    medium: str
    start_side: str
    end_side: str
    start: Pt
    end: Pt
    path: tuple[Pt,...]
    length: float
    elbows: int
    status: str
    reason: str = ''
    target_kind: str = 'element'
    depends_on: str = ''  # legacy compatibility; MUST be empty in a Связь catalogue

@dataclass
class Survey:
    project: Project
    frame: Frame
    elements: list[Element]
    wells: list[WellPlacement]
    center: Pt
    tie: Pt
    fire: Pt
    axis_end: float
    lanes: dict[str,float]
    trunk_side: int
    trunk_segments: tuple[Segment,...] = ()
    plate_variants: tuple[tuple[str,PlateGeometry],...] = ()
    side_votes: tuple[int,int] = (0,0)


def majority_trunk_side(p:Project,wells:list[WellPlacement],tie:Pt,fire:Pt):
    """Выбрать сторону по количеству физических элементов вне оси НДС.

    Осевые скважины не голосуют: их V=0. При явном выборе стороны или
    положения плиты ручное ограничение приоритетно; при равенстве — +V.
    """
    count_pos=sum(w.point.v>1e-5 for w in wells if not w.well.on_axis)
    count_neg=sum(w.point.v< -1e-5 for w in wells if not w.well.on_axis)
    for point in (tie,fire):
        count_pos+=point.v>1e-5
        count_neg+=point.v< -1e-5
    if p.trunk_side=='positive':return 1,(count_pos,count_neg)
    if p.trunk_side=='negative':return -1,(count_pos,count_neg)
    if p.center_position in ('left_upper','right_upper'):
        return 1,(count_pos,count_neg)
    if p.center_position in ('left_lower','right_lower'):
        return -1,(count_pos,count_neg)
    if p.center_x is not None:
        # Координата центра задана инженером — сохраняем знак его положения.
        cv=Frame(p).uv(p.center_x,p.center_y).v
        if abs(cv)>1e-5:return (1 if cv>0 else -1),(count_pos,count_neg)
    return (1 if count_pos>=count_neg else -1),(count_pos,count_neg)


def make_survey(p: Project) -> Survey:
    p.validate()
    frame=Frame(p)
    wells=[]
    axis_i=0
    for w in p.wells:
        if w.on_axis:
            q=Pt(axial_u(axis_i),0.)
            wx,wy=frame.xy(q)
            axis_i+=1
        else:
            wx,wy=float(w.x),float(w.y)
            q=frame.uv(wx,wy)
        wells.append(WellPlacement(w,q,wx,wy,tuple(well_connections(w)),w.orientation))
    tie=frame.uv(p.tie_x,p.tie_y)
    fire=frame.uv(p.fire_x,p.fire_y)
    # Не подменять выбранный пользователем хребет при несовместимых
    # ручных ограничениях: явно сообщить конфликт вместо скрытого переноса.
    if p.trunk_side!='auto' and p.center_position in (
        'left_upper','right_upper','left_lower','right_lower'):
        requested=1 if p.center_position.endswith('upper') else -1
        explicit=1 if p.trunk_side=='positive' else -1
        if explicit!=requested:
            raise ValueError('Заданные сторона хребта и положение центральной плиты '
                             'противоречат друг другу: верх/низ должны совпадать.')
    sign,votes=majority_trunk_side(p,wells,tie,fire)
    lanes=lane_positions(p,sign)
    last_axial=max((wp.point.u for wp in wells if wp.well.on_axis),default=0.)
    left_center=Pt(-p.head_lead,lanes['oil_measure'])
    right_center=Pt(last_axial+p.head_lead,lanes['oil_measure'])
    if p.center_x is not None:
        fixed_center=frame.uv(p.center_x,p.center_y)
        if fixed_center.u>last_axial+1e-5:
            right_center=fixed_center
        else:
            left_center=fixed_center
    left_head=left_center.u+3.0
    right_head=right_center.u-3.0
    left_plate=make_plate(left_head,lanes,PIPE_OD)
    canonical_right=make_plate(right_head+6.0,lanes,PIPE_OD)
    right_plate=PlateGeometry(right_head,right_head+6.0,
                             canonical_right.v_min,canonical_right.v_max,
                             right_head,right_head+6.0)
    variants=(('left',left_plate),('right',right_plate))
    chosen_right=p.center_position in ('right_upper','right_lower')
    if p.center_x is not None:
        chosen_right=frame.uv(p.center_x,p.center_y).u>last_axial+1e-5
    center=right_center if chosen_right else left_center
    head=right_head if chosen_right else left_head
    fixed_reaches=base_axial_trunk_reaches(wells,direction=-1 if chosen_right else 1)
    # К обоим внутренним торцам возможных центральных плит подходит
    # общий базовый хребет. Оба варианта должны быть физически доступны
    # без перемещения готовой осевой магистрали.
    other_head=left_head if chosen_right else right_head
    fixed_reaches={key:(min(u,other_head) if chosen_right else max(u,other_head))
                   for key,u in fixed_reaches.items()}
    trunk=[]
    for medium in MEDIA:
        active=fixed_reaches.get((medium,'active'))
        future=fixed_reaches.get((medium,'future'))
        if active is not None:
            trunk.append(Segment(Pt(head,lanes[medium]),Pt(active,lanes[medium]),
                                 medium,'active','Хребет'))
        if future is not None:
            # Показывать перспективный хвост, не дублируя действующую трубу.
            if active is None:
                trunk.append(Segment(Pt(head,lanes[medium]),Pt(future,lanes[medium]),
                                     medium,'future','Перспективный хребет'))
            elif (future-active)*(-1 if chosen_right else 1)>1e-8:
                trunk.append(Segment(Pt(active,lanes[medium]),Pt(future,lanes[medium]),
                                     medium,'future','Перспективное продление хребта'))
    elements=[Element(f'well:{wp.well.number}',f'Скважина №{wp.well.number}',wp.point,
                      tuple(m for m,_ in wp.connections if m!='oil_measure'),'well',wp.well.number,wp.well.status)
              for wp in wells]
    elements += [Element('tie','Точка врезки',tie,('oil_product',),'tie'),
                 Element('fire','Пожарный узел',fire,(p.fire_medium,),'fire')]
    axis_end=last_axial+18.
    return Survey(p,frame,elements,wells,center,tie,fire,axis_end,lanes,sign,
                  tuple(trunk),variants,votes)


def preview_result(survey:Survey) -> Result:
    """Несдвигаемая часть хребта до расчёта ответвлений и плит."""
    p=survey.project
    stats={m:{'active':0.,'future':0.,'total':0.} for m in MEDIA}
    for segment in survey.trunk_segments:
        stats[segment.medium][segment.phase]+=segment.length
        stats[segment.medium]['total']+=segment.length
    head=next((plate.trunk_face_u for kind,plate in survey.plate_variants
               if (kind=='right')==(survey.center.u>max((wp.point.u for wp in survey.wells
                                                        if wp.well.on_axis),default=0.))),survey.center.u)
    return Result(p,survey.frame,survey.wells,survey.center,survey.tie,survey.fire,
                  list(survey.trunk_segments),[],survey.lanes,stats,[],[],head,survey.axis_end,{},
                  {'pipe_m':sum(s.length for s in survey.trunk_segments),'optimized_score':0.},
                  [],survey.trunk_side,
                  selected_center_position=('right_' if survey.center.u>max((wp.point.u for wp in survey.wells if wp.well.on_axis),default=0.) else 'left_')+('upper' if survey.trunk_side==1 else 'lower'),
                  central_plate=None)


def flow_role(el:Element, medium:str, survey:Survey):
    if el.kind=='tie':return 'sink'
    if el.kind=='fire':return 'sink'
    well=next((wp.well for wp in survey.wells if wp.well.number==el.well_number),None)
    if well is None:return None
    return next(('source' if direction=='к центру' else 'sink'
                 for med,direction in well_connections(well) if med==medium),None)


def forced_lead(q:Pt, direction:str, amount:float)->Pt:
    du,dv=DIRECTION[direction]
    return Pt(q.u+amount*du,q.v+amount*dv)

def port(q:Pt, direction:str, *, hu:float=0.5, hv:float=0.5, stand=0.2) -> Pt:
    du,dv=DIRECTION[direction]
    return Pt(q.u+du*(hu+stand),q.v+dv*(hv+stand))


def element_port(el:Element, direction:str, medium:str) -> Pt:
    if el.kind!='well':
        return port(el.center,direction,hu=0.0,hv=0.0,stand=0.35)
    # Поворот прямоугольника бесплатный: сторона выхода определяет ориентацию.
    if direction[0]=='u': hu,hv=.8,2.
    else: hu,hv=2.,.8
    return port(el.center,direction,hu=hu,hv=hv)


def _compact(nodes):
    out=[]
    for p in nodes:
        if out and abs(p.u-out[-1].u)<1e-8 and abs(p.v-out[-1].v)<1e-8:
            continue
        out.append(p)
    return tuple(out)


def shortest_paths(start:Pt,end:Pt,placements,owner=None, *, check_guards=True,
                   inner_zone=None):
    """Независимые кратчайшие ортогональные пути между *заданными портами*.

    Сначала проверяются оба Г-образных пути минимальной манхэттенской длины.
    Если оба не проходят охранные зоны, ищется кратчайший безопасный путь
    в конечном графе видимости, построенном по краям защитных зон.

    Важно: граф конечен, поэтому эта функция не доказывает оптимальность
    относительно ВСЕХ непрерывных допустимых точек; результат нельзя
    называть глобальным минимумом общей трубопроводной сети.
    """
    routes=[]
    for mid in (Pt(start.u,end.v),Pt(end.u,start.v)):
        path=_compact((start,mid,end))
        if check_guards and any((not protected_line_ok(a,b,placements,owner=owner)
                                 or inner_trunk_parallel(a,b,inner_zone))
                                for a,b in zip(path,path[1:])):
            continue
        if path not in routes:routes.append(path)
    if routes or not check_guards:
        return routes
    if inner_zone is not None:
        sign,outer,_,_=inner_zone
        safe_v=sign*(outer+0.6)
        mid=_compact((start,Pt(start.u,safe_v),Pt(end.u,safe_v),end))
        if all(protected_line_ok(a,b,placements,owner=owner)
               and not inner_trunk_parallel(a,b,inner_zone)
               for a,b in zip(mid,mid[1:])):
            return [mid]
    # Непосредственный кратчайший маршрут заблокирован. В старом режиме v60
    # такая пара просто терялась, хотя безопасный обход может существовать.
    try:
        detour=rectilinear_safe_path(start,end,placements,owner=owner,
                                    bend_penalty=0.0,inner_zone=inner_zone)
    except ValueError:
        return []
    route=_compact(detour)
    # Последняя явная проверка ВСЕХ сегментов; не возвращать кандидаты,
    # пересекающие запретную окружность.
    if any((not protected_line_ok(a,b,placements,owner=owner)
            or inner_trunk_parallel(a,b,inner_zone))
           for a,b in zip(route,route[1:])):
        return []
    return [route]


def _axial_nearest_trunk(survey:Survey, wp:WellPlacement, medium:str, lead:Pt):
    """Единственная ближайшая физическая точка СВОЕЙ нитки хребта.

    Точка выбирается проекцией на непрерывный отрезок трубы, а не
    перебором произвольных концов, соседних скважин или портов плит.
    У действующей отпайки нет права подключаться к ТОЛЬКО перспективной
    нитке. Перспективная отпайка может использовать действующий хребет.
    """
    phase=line_phase(wp,medium)
    choices=[]
    for seg in survey.trunk_segments:
        if seg.medium!=medium or (phase=='active' and seg.phase!='active'):
            continue
        if abs(seg.a.v-seg.b.v)>1e-7:
            continue
        lo,hi=sorted((seg.a.u,seg.b.u))
        u=max(lo,min(hi,lead.u))
        point=Pt(u,seg.a.v)
        length=abs(lead.u-u)+abs(lead.v-point.v)
        # При совпадении реального и перспективного участка выбранная
        # подключаемая в данный момент нитка имеет преимущество.
        phase_preference=0 if seg.phase=='active' else 1
        choices.append((round(length,8),phase_preference,
                        round(abs(u-lead.u),8),u,point))
    if not choices:
        return None
    return min(choices,key=lambda row:row[:4])[-1]


def _axial_links(survey:Survey, source:Element, wp:WellPlacement):
    """Ровно один вариант на среду для осевой скважины.

    Отдельные порты сред и направление выхода совпадают с рабочей
    трассировкой routing.route_of_well. Не рассматриваем боковые порты
    центральных плит, другие скважины, разворот выхода и пучки.
    """
    outlets=well_medium_outlets(wp,survey.trunk_side,survey.center,
                                survey.project.lane_gap)
    normal=well_connection_normal(wp,survey.trunk_side,survey.center)
    if abs(normal.u)>0.5:
        side='u+' if normal.u>0 else 'u-'
    else:
        side='v+' if normal.v>0 else 'v-'
    links=[]
    for medium,_ in wp.connections:
        if medium=='oil_measure':
            continue  # замерная нитка не имеет отдельной устьевой отпайки
        start=outlets[medium]
        lead=Pt(start.u+normal.u*WELL_MIN_STRAIGHT,
                start.v+normal.v*WELL_MIN_STRAIGHT)
        target=_axial_nearest_trunk(survey,wp,medium,lead)
        if target is None:
            target=Pt(lead.u,survey.lanes[medium])
            links.append(LinkCandidate(source.key,f'base:trunk:{medium}:missing',medium,
                                       side,'trunk',start,target,(),0.,0,'недопустим',
                                       'Для данной среды не существует допустимой базовой нитки хребта',
                                       'trunk'))
            continue
        key=f'base:trunk:{medium}:{target.u:.5f}'
        lead_ok=protected_line_ok(start,lead,survey.wells,owner=wp.well.number)
        paths=shortest_paths(lead,target,survey.wells,owner=wp.well.number) if lead_ok else []
        if not paths:
            links.append(LinkCandidate(source.key,key,medium,side,'trunk',start,target,
                                       (),0.,0,'недопустим',
                                       'Ближайшая точка хребта заблокирована защитной зоной; '
                                       'альтернативные места для осевой скважины не подбираются',
                                       'trunk'))
            continue
        def as_path(middle):
            return _compact((start,*middle))
        def turns(path):
            return sum(1 for a,b,c in zip(path,path[1:],path[2:])
                       if (abs(b.u-a.u)>1e-7 and abs(c.v-b.v)>1e-7) or
                          (abs(b.v-a.v)>1e-7 and abs(c.u-b.u)>1e-7))
        # Даже при двух равно-кратчайших геометриях отображается ОДНА;
        # однозначный приоритет: меньше отводов, затем координаты.
        path=min((as_path(mid) for mid in paths),
                 key=lambda a:(round(sum(abs(b.u-c.u)+abs(b.v-c.v)
                                            for b,c in zip(a,a[1:])),8),
                               turns(a),tuple((round(p.u,8),round(p.v,8)) for p in a)))
        length=sum(abs(b.u-a.u)+abs(b.v-a.v) for a,b in zip(path,path[1:]))
        links.append(LinkCandidate(source.key,key,medium,side,'trunk',start,target,
                                   path,length,turns(path),'базовый хребет',
                                   'Единственная ближайшая точка хребта данной среды',
                                   'trunk'))
    return links


def virtual_targets(survey:Survey,medium:str,origin:Element):
    """Только осевые цели хребта. Внешние порты/врезка отключены."""
    if medium=='oil_measure':return []
    if origin.kind=='well':
        wp=next((w for w in survey.wells
                 if w.well.number==origin.well_number),None)
        if wp is not None and wp.well.on_axis and medium in dict(wp.connections):
            outlet=well_medium_outlets(wp,survey.trunk_side,survey.center,
                                       survey.project.lane_gap)[medium]
            normal=well_connection_normal(wp,survey.trunk_side,survey.center)
            lead=Pt(outlet.u+normal.u*WELL_MIN_STRAIGHT,
                    outlet.v+normal.v*WELL_MIN_STRAIGHT)
            point=_axial_nearest_trunk(survey,wp,medium,lead)
            return ([] if point is None else [
                (f'base:trunk:{medium}:{point.u:.5f}',point,
                 'Ближайшая точка фиксированного хребта')])
    return []


def _tie_links(survey:Survey, source:Element, limit=None):
    """ВНЕШНЯЯ ВРЕЗКА ОТКЛЮЧЕНА — алгоритм будет переработан отдельно."""
    return []


def _offaxis_exit_ports(survey:Survey, wp:WellPlacement, medium:str):
    """ВНЕОСЕВЫЕ ПОДКЛЮЧЕНИЯ ОТКЛЮЧЕНЫ."""
    return ()


def _independent_lattice_paths(start, end, survey, source_key):
    """ВНЕОСЕВЫЕ ПОДКЛЮЧЕНИЯ ОТКЛЮЧЕНЫ."""
    return ()


def _offaxis_links(survey:Survey,source:Element,limit=None):
    """ВНЕОСЕВЫЕ ПОДКЛЮЧЕНИЯ ОТКЛЮЧЕНЫ — алгоритм будет переработан отдельно."""
    return []


def _fire_links(survey:Survey, source:Element, limit=None):
    """Точка пожарного узла: две возможные торцевые точки, без сторон выхода."""
    from fire_connection_rules import fire_ends,fire_candidate_paths,route_length,route_bends
    links=[]
    inner_zone=trunk_inner_zone(survey.lanes,survey.trunk_side,
                min((min(s.a.u,s.b.u) for s in survey.trunk_segments),default=0.),
                survey.wells)
    for name,plate in survey.plate_variants:
        for endpoint in fire_ends(plate,survey.lanes,survey.trunk_segments,
                                 survey.project.fire_medium):
            key=(f'virtual:plate:{name}:fire:{source.media[0]}' if endpoint.kind=='plate'
                 else f'base:trunk:{name}:fire:{source.media[0]}:{endpoint.point.u:.4f}')
            status=('гипотеза плиты' if endpoint.kind=='plate' else 'базовый хребет')
            paths=fire_candidate_paths(endpoint,source.center)
            safe=[route for route in paths if all(
                   protected_line_ok(a,b,survey.wells) and
                   not inner_trunk_parallel(a,b,inner_zone)
                   for a,b in zip(route,route[1:]))]
            if not safe:
                links.append(LinkCandidate(source.key,key,source.media[0],'point','end',
                          source.center,endpoint.point,(),0.,0,'недопустим',
                          'Нет безопасного выхода из разрешённого торца',
                          'virtual' if endpoint.kind=='plate' else 'trunk'))
                continue
            # Прямое продолжение до проекции имеет приоритет над обходом.
            for route in sorted(safe,key=lambda r:(round(route_length(r),6),route_bends(r)))[:1]:
                links.append(LinkCandidate(source.key,key,source.media[0],'point','end',
                         source.center,endpoint.point,tuple(reversed(route)),
                         route_length(route),route_bends(route),status,
                         endpoint.name+'; независимый пожарный вывод',
                         'virtual' if endpoint.kind=='plate' else 'trunk'))
    out=sorted(links,key=lambda q:(not bool(q.path),q.length,q.elbows,q.target))
    return out if limit is None else out[:limit]




def route_id(link: LinkCandidate) -> str:
    """Stable identity of one exact precomputed route in 'Связь'."""
    serial=(link.source,link.medium,link.target,
            tuple((round(p.u,6),round(p.v,6)) for p in link.path))
    return sha1(repr(serial).encode('utf-8')).hexdigest()[:20]


def _generate_links_unfiltered(survey:Survey, selected_key:str, *, limit: int|None=None) -> list[LinkCandidate]:
    """Только осевое ядро и пожарный узел.

    Внеосевые скважины и точка врезки: связей нет (алгоритм внешних трасс
    снят для переработки). Старые переборы портов/пучков не вызываются.
    """
    by_key={e.key:e for e in survey.elements}
    source=by_key.get(selected_key)
    if source is None:return []
    if source.status=='inactive':return []
    if source.kind=='tie':
        return []  # внешняя врезка отключена
    if source.kind=='fire':
        return _fire_links(survey,source,limit)
    if source.kind=='well':
        axial_wp=next((wp for wp in survey.wells
                       if wp.well.number==source.well_number and wp.well.on_axis),None)
        if axial_wp is not None:
            candidates=_axial_links(survey,source,axial_wp)
            return candidates if limit is None else candidates[:limit]
        return []  # внеосевые подключения отключены
    return []



def generate_links(survey:Survey, selected_key:str, *, limit: int|None=None) -> list[LinkCandidate]:
    """The exact same 1.5m no-transit zone applies to Связь and Рассчитать.

    A rejected route is displayed with a reason but cannot enter optimization.
    We do NOT change its geometry; the connection catalogue remains the only
    source of pipeline paths.
    """
    from structural_clearance import clearance_violations
    links=_generate_links_unfiltered(survey,selected_key,limit=None)
    if any(q.depends_on or q.target.startswith('dep:') or q.target_kind=='dependent' for q in links):
        raise ValueError('«Связь» не имеет права рассматривать объединение линий разных элементов')
    checked=[]
    for q in links:
        if not q.path or q.status=='недопустим':
            checked.append(q);continue
        # A route can belong to a particular virtual plate, or to either
        # preliminary plate when the target has no explicit variant.
        variants=[(name,plate) for name,plate in survey.plate_variants
                  if f':{name}:' in q.target]
        if not variants:
            variants=list(survey.plate_variants)
        faults=[clearance_violations(q,survey,plate) for _,plate in variants]
        if faults and all(faults):
            reason='; '.join(dict.fromkeys(faults[0]))
            checked.append(replace(q,status='недопустим',reason=reason))
        else:
            checked.append(q)
    return checked if limit is None else checked[:limit]

def link_stats(links):
    return {'total':len(links),
            'shortest':sum(x.status=='кратчайший' for x in links),
            'hypothetical':sum(x.target_kind=='virtual' and bool(x.path) for x in links),
            'trunk':sum(x.target_kind=='trunk' and bool(x.path) for x in links),
            'rejected':sum(x.status=='недопустим' for x in links),
            'detours':sum(bool(x.path) and 'обход' in x.reason.lower() for x in links)}
