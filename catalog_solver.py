"""v83. Strict connection-catalog route selection and fixed-first support optimization.

No routing function runs here: the ONLY nonstructural pipe geometries used
are complete LinkCandidate.path polylines produced by the 'Связь' stage.
The original routings' grouping/tee/nudging functions are intentionally not
called: all of them can manufacture geometry which is absent from the catalog.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, replace
from math import inf

from routing import (Project, Result, Pt, Segment, MEDIA, PIPE_OD,
    collision_notes, forbidden_foreign_contacts, protected_well_conflicts,
    inner_trunk_transit_conflicts, trunk_inner_zone)
from planning_workflow import (Survey, LinkCandidate, generate_links, make_survey, route_id)
from central_plate import central_pipe_segments
from product_tie_rules import preview_support_slabs
from network_cost import network_fittings, count_small_plates, estimate_score, transit_corridor_overlap

# Beam-search is deliberately approximate, but all paths in it are catalogued.
BEAM_WIDTH = 120
GROUP_LIMIT = 45


def path_signature(path):
    return tuple((round(p.u, 6), round(p.v, 6)) for p in path)


def link_signature(link: LinkCandidate):
    return (link.source, link.medium, link.target, path_signature(link.path))


def _central_variant(link: LinkCandidate):
    parts = link.target.split(':')
    if len(parts) >= 4 and parts[:2] == ['virtual', 'plate']:
        return parts[2] if parts[2] in ('left', 'right') else None
    if len(parts) >= 4 and parts[:2] == ['base', 'trunk']:
        for name in parts[2:]:
            if name in ('left', 'right'):
                return name
    return None


def _available(link, name):
    return bool(link.path) and link.status != 'недопустим' and (_central_variant(link) in (None, name))


def _link_cost(q, p):
    return p.pipe_cost_per_m*q.length + p.elbow_cost*q.elbows


def _exact_shared_length(a, b, *, phase_a='active', phase_b='active'):
    """Length of a real, same-medium collinear overlap in EXISTING routes.

    Near-parallel lines have no physical tee and are not a merged pipe.
    Fire and different-phase lines cannot share a pipe with a well line.
    """
    if a.source==b.source or a.medium!=b.medium or phase_a!=phase_b:
        return 0.0
    if 'fire' in (a.source,b.source):
        return 0.0
    intervals=[]
    for x,y in zip(a.path,a.path[1:]):
        for v,w in zip(b.path,b.path[1:]):
            if abs(x.v-y.v)<1e-7 and abs(v.v-w.v)<1e-7 and abs(x.v-v.v)<1e-6:
                lo=max(min(x.u,y.u),min(v.u,w.u));hi=min(max(x.u,y.u),max(v.u,w.u))
                if hi-lo>1e-6:intervals.append(('u',round(x.v,6),lo,hi))
            elif abs(x.u-y.u)<1e-7 and abs(v.u-w.u)<1e-7 and abs(x.u-v.u)<1e-6:
                lo=max(min(x.v,y.v),min(v.v,w.v));hi=min(max(x.v,y.v),max(v.v,w.v))
                if hi-lo>1e-6:intervals.append(('v',round(x.u,6),lo,hi))
    # Union intervals to avoid counting one shared line multiple times.
    total=0.0
    for axis,pos in set((axis,pos) for axis,pos,_,_ in intervals):
        runs=sorted((lo,hi) for a,p,lo,hi in intervals if a==axis and p==pos)
        low=high=None
        for lo,hi in runs:
            if low is None:low,high=lo,hi
            elif lo<=high+1e-6:high=max(high,hi)
            else:total+=high-low;low,high=lo,hi
        if low is not None:total+=high-low
    return total


def _existing_shared_lines(selected, survey):
    """Diagnostic inventory: detected shared pipe, never planned new branches."""
    result=[]
    for i,a in enumerate(selected):
        for b in selected[i+1:]:
            pa=_phase(survey,a);pb=_phase(survey,b)
            shared=_exact_shared_length(a,b,phase_a=pa,phase_b=pb)
            if shared>1e-5:
                result.append({'sources':(a.source,b.source), 'medium':a.medium,
                               'shared_m':round(shared,4),
                               'status':'совпадающие участки существующих связей'})
    return result


def _choice_pool(available, p):
    """Keep best links for multiple destination families, never invent a path."""
    ranked = sorted(available, key=lambda q:(_link_cost(q,p), q.elbows,q.length,q.target,q.start_side,
                                            path_signature(q.path)))
    families=defaultdict(list)
    for item in ranked:
        family=('plate' if item.target.startswith('virtual:plate:') else
                'far_end' if ':end:' in item.target or ':fire:' in item.target else 'trunk')
        if len(families[family])<8:
            families[family].append(item)
    pool={link_signature(q):q for rows in families.values() for q in rows}
    # Preserving distinct destination coordinates is essential: a nearby
    # alternative can reverse the order of two parallel transit pipes and
    # eliminate an otherwise unnecessary crossing at the side approach.
    # No new route is generated: all these candidates belong to «Связь».
    for family in ('plate','trunk'):
        same=[q for q in ranked if (('plate' if q.target.startswith('virtual:plate:') else
                                    'far_end' if ':end:' in q.target else 'trunk')==family)
]
        if not same:continue
        center=same[0].end.u
        for shift in (-1.2,-0.9,-0.6,-0.3,0.3,0.6,0.9,1.2):
            near=(q for q in same if abs(q.end.u-(center+shift))<0.17)
            candidate=min(near,key=lambda q:(_link_cost(q,p),q.elbows),default=None)
            if candidate is not None:pool[link_signature(candidate)]=candidate
    return sorted(pool.values(),key=lambda q:(_link_cost(q,p),q.elbows,q.target))[:GROUP_LIMIT]


def _purpose(q):
    if q.source=='tie':
        return 'Продуктовый нефтепровод: от центра к врезке'
    if q.source=='fire':
        return 'Подключение пожарного узла'
    if q.source.startswith('well:'):
        return 'Отвод от скв. '+q.source.split(':')[1]
    return 'Каталожная связь'


def _route_segments(q, phase):
    return [Segment(a,b,q.medium,phase,_purpose(q))
            for a,b in zip(q.path,q.path[1:]) if abs(a.u-b.u)+abs(a.v-b.v)>1e-7]


def _phase(survey,q):
    if not q.source.startswith('well:'):
        return 'active'
    number=int(q.source.split(':')[1])
    wp=next(w for w in survey.wells if w.well.number==number)
    return 'future' if wp.well.status=='future' else 'active'


def _groups(survey,catalog):
    problems=[]; groups=[]
    # Reject obsolete or externally supplied dependent paths; do not hide them.
    if any(q.depends_on or q.target.startswith('dep:') or q.target_kind=='dependent'
           for variants in catalog.values() for q in variants):
        raise ValueError('Обнаружены зависимые связи от старого алгоритма. '
                         'Повторите этап «Связь»: маршруты должны быть независимыми.')
    axial_ids={wp.well.number for wp in survey.wells if wp.well.on_axis}
    for el in survey.elements:
        if el.status=='inactive':continue
        # External routing (off-axis wells + product tie) is disabled.
        if el.kind=='tie':
            continue
        if el.kind=='well' and el.well_number not in axial_ids:
            continue
        required=el.media
        for medium in required:
            valid=[c for c in catalog.get(el.key,()) if c.medium==medium and c.path
                   and c.status!='недопустим']
            if not valid:
                bad=[c.reason for c in catalog.get(el.key,()) if c.medium==medium and c.reason]
                problems.append(f'{el.label}, {medium}: '+ (bad[0] if bad else 'нет допустимых связей'))
            else:
                groups.append(((el.key,medium),valid))
    if problems:
        raise ValueError('Для обязательных осевых/пожарных линий нет вариантов. '
                         + '; '.join(problems[:5]))
    groups.sort(key=lambda g:(len(g[1])!=1,g[0][0]=='fire',len(g[1]),g[0]))
    return groups


def _trim_base_trunk(survey, plate, selected):
    """Trim unconnected *structural* trunk tails AFTER all link endpoints are known.

    Only the original trunk is shortened, never a catalogue route. The farthest
    mandatory attachment of each medium determines its actual installed reach.
    This is intentionally recomputed for each candidate network before costing.
    """
    from routing import Pt, Segment
    head=plate.trunk_face_u
    right=plate.trunk_face_u == plate.u_min
    wanted={}
    for q in selected:
        if q.target.startswith('base:trunk:') and not q.depends_on:
            if (q.source.startswith('well:') or q.source in ('fire','tie')):
                u=q.end.u
                if q.medium not in wanted or abs(u-head)>abs(wanted[q.medium]-head):
                    wanted[q.medium]=u
    kept=[]
    for s in survey.trunk_segments:
        if s.medium not in wanted:continue
        end=wanted[s.medium]
        # Inclusive span between central plate and farthest required tap.
        low,high=sorted((head,end))
        a,b=sorted((s.a.u,s.b.u))
        start=max(low,a);stop=min(high,b)
        if stop-start<=1e-7:continue
        if s.a.u<=s.b.u:
            aa,bb=Pt(start,s.a.v),Pt(stop,s.a.v)
        else:
            aa,bb=Pt(stop,s.a.v),Pt(start,s.a.v)
        kept.append(Segment(aa,bb,s.medium,s.phase,s.purpose))
    return kept


def _network_segs(survey, plate, selected):
    trunk=_trim_base_trunk(survey,plate,selected)
    present=tuple(sorted({s.medium for s in trunk}|{q.medium for q in selected}))
    # Plate pipes are structural parts of the plate, never independent routes.
    plate_pipes=central_pipe_segments(plate,survey.lanes,present,{m:'active' for m in present})
    routed=[]
    for q in selected:
        routed.extend(_route_segments(q,_phase(survey,q)))
    return [*trunk,*plate_pipes,*routed]



def _fire_collinear_contacts(segs):
    """A fire pipe cannot reuse an external well/tie pipe's longitudinal axis.

    The only valid connection to the common system is at a permitted trunk
    or central plate END. An isolated 90-degree crossing is not a bundle.
    """
    fire=[s for s in segs if s.purpose=='Подключение пожарного узла']
    unrelated=[s for s in segs if s.purpose not in (
        'Подключение пожарного узла',
        'Хребет', 'Перспективный хребет', 'Перспективное продление хребта',
        'Центральная плита: магистральная труба')]
    for a in fire:
        for b in unrelated:
            if a.horizontal != b.horizontal:
                continue
            if a.horizontal:
                if abs(a.a.v-b.a.v)>1e-6:continue
                overlap=min(max(a.a.u,a.b.u),max(b.a.u,b.b.u))-max(min(a.a.u,a.b.u),min(b.a.u,b.b.u))
            else:
                if abs(a.a.u-b.a.u)>1e-6:continue
                overlap=min(max(a.a.v,a.b.v),max(b.a.v,b.b.v))-max(min(a.a.v,a.b.v),min(b.a.v,b.b.v))
            if overlap>1e-5:
                return True
    return False


def _invalid_combo(survey,segs,base_foreign=0):
    if _fire_collinear_contacts(segs):
        return 'Пожарная линия совпадает с внешней трубой: общий участок/пучок запрещён'
    if protected_well_conflicts(segs,survey.wells):
        return 'Нарушены защитные зоны чужих скважин'
    zone=trunk_inner_zone(survey.lanes,survey.trunk_side,
             min((min(s.a.u,s.b.u) for s in survey.trunk_segments),default=0.), survey.wells)
    # The catalog has already checked that each independent path avoids the forbidden
    # strip, but composing paths must not introduce additional invalid geometry.
    bad=inner_trunk_transit_conflicts(segs,survey.wells,survey.lanes,
                                     survey.trunk_side, min((s.a.u for s in survey.trunk_segments),default=0.))
    if bad:
        return 'Транзит вдоль хребта со стороны скважин'
    if len(forbidden_foreign_contacts(segs, min((s.a.u for s in survey.trunk_segments),default=0.)))>base_foreign:
        return 'Запрещённое касание труб разных сред'
    fittings=network_fittings(segs)
    if fittings['fourways']:
        return 'Четырёхстороннее соединение трубы без допустимого тройника'
    return ''


def _rough_cost(selected, p):
    # Pruning is heuristic, never an authority to create/change a route.
    return sum(_link_cost(q,p) for q in selected)


def _support_slabs(survey,plate,segs,placements,selected):
    """First place all mandatory supports, THEN fill the straight intervals.

    All bends of external lines and junctions are mandatory support sites;
    the tie, fire and off-axis well terminals and supported trunk taps are
    locked as well.  Filler plates never displace or replace those supports.
    The pipe segments are only READ here and cannot be altered.
    """
    from axial_plates import create_endpoint_support_plates, create_axial_branch_support_plates
    from trunk_tap_plates import create_trunk_tap_plates
    from fitting_support import complete_support
    # The preview was built against the longer, provisional trunk. Prune
    # supports that no longer carry a physical trunk pipe after tail cutting.
    preliminary=list(preview_support_slabs(survey,plate))
    installed_trunk=[s for s in segs if s.purpose in (
        'Хребет','Перспективный хребет','Перспективное продление хребта')]
    slabs=[]
    for slab in preliminary:
        if slab.slab_type=='central':
            slabs.append(slab);continue
        carried=[]
        for s in installed_trunk:
            if s.medium in carried or not s.horizontal:continue
            lo,hi=sorted((s.a.u,s.b.u))
            if min(hi,slab.u_end)-max(lo,slab.u_start)>1e-5:
                carried.append(s.medium)
        if carried:
            slabs.append(replace(slab,media=tuple(sorted(carried)),media_count=len(carried)))
    slabs.extend(create_axial_branch_support_plates(
        placements,segs,plate_offset=survey.project.branch_plate_offset))
    slabs.extend(create_endpoint_support_plates(placements,segs,survey.tie,survey.fire))
    # A trunk tap needs a proper trunk plate before ordinary 1.2x3 supports
    # are placed.  This avoids pretending a 1.2x3 filler can hold a trunk tee.
    slabs.extend(create_trunk_tap_plates(segs,slabs,plate,strict=True))
    from structural_clearance import boundary_supports
    # Both endmost transit slabs are FIXED. Their nearest physical EDGE is
    # 1.5 m away from the main trunk or central plate, before ordinary fillers.
    boundary_fixed=boundary_supports(survey,plate,selected,slabs)
    slabs.extend(boundary_fixed)
    exact_bends=set()
    offaxis={wp.well.number for wp in placements if not wp.well.on_axis}
    for q in selected:
        if q.source.startswith('well:') and int(q.source.split(':')[1]) not in offaxis:
            continue
        for a,b,c in zip(q.path,q.path[1:],q.path[2:]):
            first_h=abs(b.v-a.v)<1e-7
            second_h=abs(c.v-b.v)<1e-7
            if first_h == second_h:
                continue  # no turn on a straight run
            exact_bends.add((round(b.u,5),round(b.v,5)))
    anchored,removed,sites,missing=complete_support(
        segs,slabs,strict=True,exact_sites=exact_bends,offaxis_well_ids=offaxis)
    if removed:
        raise ValueError('В раскладку фиксированных плит попали предварительные заполнители')
    from axial_plates import offaxis_unsupported_gaps
    gaps=offaxis_unsupported_gaps(segs,[*slabs,*anchored],offaxis)
    if gaps:
        details='; '.join(f'скв. {n}, {med}: {gap:.2f} м' for n,med,_,_,gap in gaps[:5])
        raise ValueError('Не удалось расставить плиты-заполнители на внешних линиях: '+details)
    # Independent fire system: no support slab may carry a longitudinal
    # stretch of BOTH the fire line and a well/tie line.  A perpendicular
    # crossing or a legal fire-to-trunk endpoint is not a shared corridor.
    from structural_clearance import support_clearance_violations
    bad_supports=support_clearance_violations([*slabs,*anchored],survey,plate)
    if bad_supports:
        raise ValueError('Плита транзита размещена внутри защитной полосы 1,5 м: '+
                         str(bad_supports[0]))
    from axial_plates import fire_shared_support_violations
    fire_violations=fire_shared_support_violations(segs,[*slabs,*anchored])
    if fire_violations:
        raise ValueError('Пожарная линия не может использовать общие опоры/пучки '
                         'с линиями скважин или врезки. Выберите другую связь.')
    from axial_plates import fire_unsupported_gaps
    fire_gaps=fire_unsupported_gaps(segs,[*slabs,*anchored])
    if fire_gaps:
        raise ValueError('Пожарная линия не имеет собственного ряда плит-заполнителей: '
                         f'неподдержанный участок {fire_gaps[0][-1]:.2f} м. '
                         'Использовать плиты чужого пучка запрещено.')
    return [*slabs,*anchored], {
        'fixed_support_count':sum(s.slab_type not in ('transit_run','fire_transit_run') for s in [*slabs,*anchored]),
        'boundary_fixed_plate_count':len(boundary_fixed),
        'elbow_support_site_count':sum(s.kind=='elbow' for s in sites),
        'fitting_site_count':len(sites),
        'exact_external_bend_count':len(exact_bends),
        'fitting_unsupported_count':len(missing),
        'filler_plate_count':sum(s.slab_type in ('transit_run','fire_transit_run') for s in anchored),
        'fitting_support_plate_count':sum(s.slab_type=='fitting_support' for s in anchored),
        'trunk_tap_plate_count':sum(s.slab_type=='trunk_tap_plate' for s in slabs),
        'axial_plate_count':sum(s.slab_type=='axial_junction' for s in slabs),
        'axial_branch_plate_count':sum(s.slab_type=='axial_branch' for s in slabs),
        'intermediate_trunk_plate_count':sum(s.slab_type=='trunk_intermediate' for s in slabs),
        'transit_run_plate_count':sum(s.slab_type in ('transit_run','fire_transit_run') for s in anchored),
        'endpoint_plate_count':sum(s.slab_type in ('terminal_node','offaxis_branch') for s in slabs),
    }


def _selected_well_placements(survey,selected):
    """Render a well according to its actually selected connection exit.

    A source may use multiple media, but the physical rectangle has ONE
    orientation. Opposite exit directions still share an orientation; two
    perpendicular orientations are not compatible and cannot be combined.
    """
    chosen={}
    for q in selected:
        if not q.source.startswith('well:') or q.start_side not in ('u+','u-','v+','v-'):
            continue
        number=int(q.source.split(':')[1])
        orient='perpendicular' if q.start_side.startswith('u') else 'parallel'
        old=chosen.get(number)
        if old is not None and old != orient:
            raise ValueError(f'Скважина №{number}: выбранные связи требуют разного разворота скважины')
        chosen[number]=orient
    placements=[]
    for wp in survey.wells:
        orient=chosen.get(wp.well.number,wp.orientation) if not wp.well.on_axis else wp.orientation
        if wp.well.rotation_locked and orient != wp.orientation:
            raise ValueError(f'Скважина №{wp.well.number}: запрещён поворот зафиксированной скважины')
        placements.append(replace(wp,well=replace(wp.well,orientation=orient),orientation=orient))
    return placements


def _make_result(survey, name, selected, *, check=True):
    p=survey.project
    plate=next(plate for tag,plate in survey.plate_variants if tag==name)
    if any(q.depends_on or q.target.startswith('dep:') or q.target_kind=='dependent'
           for q in selected):
        raise ValueError('Рассчитать: зависимые маршруты не допускаются; '
                         'можно выбирать и совмещать только независимые готовые связи')
    # Final per-link clearance audit: no candidate from an outdated Связь
    # catalogue can bypass the exclusion via the network solver.
    from structural_clearance import clearance_violations
    for q in selected:
        violations=clearance_violations(q,survey,plate)
        if violations:
            raise ValueError(f'{q.source} → {q.target}: {violations[0]}')
    segs=_network_segs(survey,plate,selected)
    # v89 FAIL-CLOSED: disconnected off-axis well loops must NEVER be priced.
    # In particular, a well -> well tee must have a real downstream path to
    # the central plate through the selected medium; local tee validity alone
    # does not imply that the network is connected.
    from network_integrity import audit_mandatory_connections
    integrity=audit_mandatory_connections(survey,segs,selected,
        allow_missing_fire=not any(q.source == 'fire' for q in selected))
    problem=_invalid_combo(survey,segs) if check else ''
    if problem:raise ValueError(problem)
    placements=_selected_well_placements(survey,selected)
    slabs,plate_info=_support_slabs(survey,plate,segs,placements,selected)
    # Смета измеряет ФИЗИЧЕСКИЕ нитки. Участок той же среды, одинаковой
    # геометрии и фазы оплачивается один раз, даже если им пользуются две
    # независимо выбранные связи. Разные среды оплачиваются раздельно.
    from pipe_render import make_network
    stats={m:{'active':0.,'future':0.,'total':0.} for m in MEDIA}
    for medium,edges in make_network(segs).items():
        for a,b,phase in edges:
            length=abs(a[0]-b[0])+abs(a[1]-b[1])
            stats[medium][phase]+=length
            stats[medium]['total']+=length
    physical_m=sum(item['total'] for item in stats.values())
    fittings=network_fittings(segs)
    crossings=collision_notes(segs)
    metrics={
       'pipe_m':physical_m,
       'same_medium_shared_m':max(0.,sum(s.length for s in segs)-physical_m),
       'trimmed_trunk_m':max(0.,sum(s.length for s in survey.trunk_segments)-sum(s.length for s in _trim_base_trunk(survey,plate,selected))),
       'topological_elbows':float(fittings['elbows']),
       'topological_tees':float(fittings['tees']),
       'bends':float(fittings['elbows']),
       'overlaps':float(sum('Наложение осей' in text for text in crossings)),
       'foreign_contacts':float(len(forbidden_foreign_contacts(segs,plate.trunk_face_u))),
       'fourway_count':float(len(fittings['fourways'])),
       'crossings':float(len(crossings)),
       'small_plate_count':float(count_small_plates(slabs)),
       'transit_corridor_m':transit_corridor_overlap(segs,p.lane_gap) if p.shared_corridors else 0.0,
       'shared_corridor_m':transit_corridor_overlap(segs,p.lane_gap) if p.shared_corridors else 0.0,
       **{k:float(v) for k,v in plate_info.items()},
    }
    result=Result(p,survey.frame,placements,Pt(plate.center_u,survey.lanes['oil_measure']),
        survey.tie,survey.fire,segs,slabs,survey.lanes,stats,crossings,
        ['v87: выбранные маршруты принадлежат внутреннему множеству допустимых геометрий; '
         'при оптимизации могут быть созданы новые соединения после проверки.',
         'Фиксированные опоры (узлы, концы, повороты, внешние границы зон 1,5 м) рассчитаны первыми; заполнители — только после них.',
         'Дискретный и ограниченный поиск; глобальный минимум не доказан.'],
        plate.trunk_face_u, max((max(s.a.u,s.b.u) for s in survey.trunk_segments),default=survey.axis_end),
        {},metrics,[],survey.trunk_side,
        selected_center_position=name+('_upper' if survey.trunk_side==1 else '_lower'),
        central_plate=plate)
    result.metrics['optimized_score']=estimate_score(result)
    from bundle_geometry import plate_bundle_issues
    support_issues=plate_bundle_issues(segs,slabs)
    result.metrics['bundle_plate_gap_issues']=float(len(support_issues))
    if support_issues:
        result.notes.append('ВНИМАНИЕ: на одной или нескольких плитах пучка '
                            'межосевое расстояние труб менее 0,30 м или пакет '
                            'не помещается на плиту 1,2 м. Геометрия не изменена '
                            'автоматически.')
    result.decision_trace['_integrity_audit']=integrity
    result.metrics['required_connections_checked']=float(integrity['checked'])
    result.metrics['required_connections_broken']=0.0
    result.decision_trace['_catalog_only']=True
    result.decision_trace['_existing_shared_lines']=_existing_shared_lines(selected,survey)
    result.decision_trace['_selected_links']=[dict(source=q.source,medium=q.medium,target=q.target,
        path=path_signature(q.path),length=q.length,elbows=q.elbows)
        for q in selected]
    result.decision_trace['_catalog_hashes']=[link_signature(q) for q in selected]
    result.decision_trace['_catalog_selection']={'selected':len(selected),
        'strategy':'independent_links_only_shared_existing_geometry', 'network_score':result.metrics['optimized_score']}
    result.decision_trace['_well_orientation']={wp.well.number:wp.orientation
        for wp in placements if not wp.well.on_axis}
    return result


def audit_catalog_result(result, catalog):
    """Fail closed if any selected route has no exact catalog correspondence."""
    if any(link.depends_on or link.target.startswith('dep:')
           for links in catalog.values() for link in links):
        raise ValueError('Каталог содержит чужие зависимые подключения; нажмите «Связь» заново')
    available={link_signature(link) for links in catalog.values() for link in links
               if link.path and link.status!='недопустим'}
    entries=result.decision_trace.get('_catalog_hashes')
    if entries is None:raise ValueError('Нельзя подтвердить происхождение линий: нет ссылок на «Связь».')
    invalid=[entry for entry in entries if entry not in available]
    if invalid:raise ValueError(f'В расчёте {len(invalid)} маршрутов отсутствуют в «Связь».')
    route_segments=[s for s in result.segments if s.purpose not in (
        'Хребет','Перспективный хребет','Перспективное продление хребта',
        'Центральная плита: магистральная труба')]
    selected=result.decision_trace['_selected_links']
    expected=[]
    for entry in selected:
        purpose=('Продуктовый нефтепровод: от центра к врезке' if entry['source']=='tie' else
                 'Подключение пожарного узла' if entry['source']=='fire' else
                 'Отвод от скв. '+entry['source'].split(':')[1])
        expected.extend((entry['medium'],purpose,a,b) for a,b in zip(entry['path'],entry['path'][1:])
                        if a!=b)
    actual=[(s.medium,s.purpose,(round(s.a.u,6),round(s.a.v,6)),
             (round(s.b.u,6),round(s.b.v,6))) for s in route_segments]
    if sorted(actual)!=sorted(expected):
        raise ValueError('В готовом плане появились сегменты, которых нет в выбранных связях.')
    return True


def _route_corridor_runs(q):
    """Horizontal / vertical runs of a FULL catalogue route, no invented geometry."""
    if q.source=='fire':
        return ()
    runs=[]
    for a,b in zip(q.path,q.path[1:]):
        if abs(a.u-b.u)<1e-7:
            length=abs(a.v-b.v)
            if length>=2.5:runs.append(('v',a.u,*sorted((a.v,b.v)),q.medium))
        elif abs(a.v-b.v)<1e-7:
            length=abs(a.u-b.u)
            if length>=2.5:runs.append(('h',a.v,*sorted((a.u,b.u)),q.medium))
    return tuple(runs)


def _corridor_overlap(a,b,p):
    """Conservative *potential* common support length; real slab count wins.

    Transit packs use 0.30 m AXIS spacing on a 1.2 m slab (see bundle_geometry).
    Do NOT mix in trunk lane_gap (free surface gap on the 1.75 m spine): that
    wrongly rejected legal 0.30 m packs of DN159 media and hid plate savings.
    """
    from bundle_geometry import AXIS_GAP, TRANSIT_WIDTH
    if a.source=='fire' or b.source=='fire' or a.source==b.source:
        return 0.
    ra=_route_corridor_runs(a);rb=_route_corridor_runs(b)
    total=0.
    for axis_a,cross_a,lo_a,hi_a,med_a in ra:
        for axis_b,cross_b,lo_b,hi_b,med_b in rb:
            if axis_a!=axis_b:continue
            separation=abs(cross_a-cross_b)
            # Axis-to-axis window that still fits both full ODs on one 1.2 m plate.
            max_sep=TRANSIT_WIDTH-(PIPE_OD.get(med_a,.1)+PIPE_OD.get(med_b,.1))/2
            if separation<AXIS_GAP-1e-6 or separation>max_sep+1e-6:
                continue
            overlap=max(0.,min(hi_a,hi_b)-max(lo_a,lo_b))
            if overlap>2.0:total+=overlap
    return total


def _improvement_links(variants, current, other_selected, project, name, limit=15):
    """Include less-direct catalogue routes that may share a real row of slabs.

    Never creates or shifts a path.  Adds alternatives that the old beam
    removed because their standalone length was slightly larger.
    """
    available=[q for q in variants if _available(q,name)]
    if len(available)<=limit:return available
    seen={}
    direct=sorted(available,key=lambda q:(_link_cost(q,project),q.elbows,q.target))[:4]
    for q in [current,*direct]:seen[link_signature(q)]=q
    if not project.shared_corridors or current.source=='fire':
        return list(seen.values())
    # If two routes can fit on ONE 1.2 m support, the maximum potential
    # saving is one 12k slab per ~5 m of common run (3 m slab+2 m gap).
    # Actual complete fixed-first slab placement is always checked later.
    measured=[]
    for q in available:
        support_potential=sum(_corridor_overlap(q,other,project) for other in other_selected)
        shared_pipe_potential=sum(_exact_shared_length(q,other) for other in other_selected)
        if support_potential < 2.5 and shared_pipe_potential < 0.5:continue
        extra=_link_cost(q,project)-_link_cost(current,project)
        # Screening only. Full physical length/plates/tees are recalculated
        # for every accepted candidate, never an invented corridor discount.
        indicator=extra-(support_potential*project.plate_cost/5.0+
                         shared_pipe_potential*project.pipe_cost_per_m)
        potential=support_potential+shared_pipe_potential
        measured.append((indicator,-potential,_link_cost(q,project),q))
    measured.sort(key=lambda t:t[:3])
    for _,__,___,q in measured[:limit-len(seen)]:seen[link_signature(q)]=q
    return list(seen.values())


def _external_link_crossings(selected,axial_ids=()):
    """Pairs of independent external links that cross each other in plan.

    The structural trunk, plate and fire line are intentionally excluded:
    this is only a sequencing preference for independent transit corridors.
    """
    from routing import intersect
    axial_ids=set(axial_ids)
    external=[q for q in selected if q.source!='fire' and not (q.source.startswith('well:') and int(q.source.split(':')[1]) in axial_ids)]
    pairs=[]
    for i,a in enumerate(external):
        for b in external[i+1:]:
            if a.medium==b.medium or a.source==b.source:continue
            for sa in _route_segments(a,'active'):
                for sb in _route_segments(b,'active'):
                    kind,_=intersect(sa,sb)
                    if kind=='cross':
                        key=tuple(sorted((a.source,b.source)))
                        pairs.append(key)
    return tuple(sorted(set(pairs)))


def _improve_transit_order(survey,catalog,result,max_trials=24):
    """Prefer a less tangled ordering of independent pipes where economical.

    Before comparing complete feasible networks, try swapping only routes that
    already exist in «Связь». Allow at most 1 m of added pipe value per
    avoided crossing: an ENGINEERING preference, not a hidden item in the
    material estimate. The total geometric change is also limited to 1.5 m
    at each target. This cannot modify structural trunk/central plate pipes.
    """
    name='right' if result.selected_center_position.startswith('right') else 'left'
    by_sig={link_signature(q):q for group in catalog.values() for q in group}
    current=[by_sig[s] for s in result.decision_trace['_catalog_hashes']]
    axial_ids={wp.well.number for wp in survey.wells if wp.well.on_axis}
    before=_external_link_crossings(current,axial_ids)
    original_price=estimate_score(result)
    if not before:
        result.metrics['avoidable_transit_crossings']=0.0
        result.metrics['transit_order_trials']=0.0
        return result
    def candidates(q):
        if q.depends_on or any(item.depends_on==route_id(q) for item in current):
            return [q]
        options=[]
        for v in catalog.get(q.source,()):
            if (v.medium!=q.medium or v.depends_on or not _available(v,name)
                or v.start_side!=q.start_side or not v.path or
                abs(v.end.u-q.end.u)+abs(v.end.v-q.end.v)>1.5 or
                abs(v.length-q.length)>2.0):
                continue
            options.append(v)
        options.sort(key=lambda v:(len(_external_link_crossings(
                         [v,*[x for x in current if x.source!=q.source]],axial_ids)),
                         abs(_link_cost(v,survey.project)-_link_cost(q,survey.project)),
                         abs(v.end.u-q.end.u)+abs(v.end.v-q.end.v)))
        options=[q,*[v for v in options if link_signature(v)!=link_signature(q)][:4]]
        return options
    attempts=0
    for _ in range(2):
        crosses=_external_link_crossings(current,axial_ids)
        if not crosses:break
        candidate_best=None
        for first,second in crosses[:3]:
            ii=next((i for i,q in enumerate(current) if q.source==first),None)
            jj=next((i for i,q in enumerate(current) if q.source==second),None)
            if ii is None or jj is None:continue
            # Try both unilateral changes and paired swaps. No donor geometry
            # can be silently invalidated; _make_result checks dependencies.
            opts_i=candidates(current[ii]); opts_j=candidates(current[jj])
            short=[]
            for ai in opts_i:
                for bj in opts_j:
                    if ai is current[ii] and bj is current[jj]:continue
                    temp=current.copy();temp[ii]=ai;temp[jj]=bj
                    new_cross=_external_link_crossings(temp,axial_ids)
                    if len(new_cross)>=len(crosses):continue
                    extra=sum(_link_cost(q,survey.project) for q in temp)-sum(
                          _link_cost(q,survey.project) for q in current)
                    # Keep near-identical geometry and avoid a large reroute.
                    if extra>survey.project.pipe_cost_per_m*2.0:continue
                    short.append((len(new_cross),extra,temp))
            short.sort(key=lambda z:(z[0],z[1]))
            for new_cross_count,_extra,trial_links in short[:7]:
                if attempts>=max_trials:break
                attempts+=1
                try:
                    trial=_make_result(survey,name,trial_links)
                    audit_catalog_result(trial,catalog)
                except (ValueError,AssertionError):continue
                price=estimate_score(trial)
                max_extra=(len(crosses)-new_cross_count)*survey.project.pipe_cost_per_m
                if price>estimate_score(result)+max_extra+1e-5:continue
                rank=(new_cross_count,round(price,4))
                if candidate_best is None or rank<candidate_best[0]:
                    candidate_best=(rank,trial,trial_links)
            if attempts>=max_trials:break
        if candidate_best is None:break
        _,result,current=candidate_best
    after=_external_link_crossings(current,axial_ids)
    result.metrics['avoidable_transit_crossings']=float(len(after))
    result.metrics['transit_order_trials']=float(attempts)
    result.metrics['transit_order_avoided_crossings']=float(len(before)-len(after))
    if len(after)<len(before):
        result.notes.append(f'Очередность транзитных труб: устранено {len(before)-len(after)} '
                            'пересечений за счёт уже существующих вариантов «Связь»; '
                            'финальная денежная стоимость пересчитана по фактическим материалам.')
    else:
        result.notes.append(f'Очередность транзитных труб: {len(after)} пересечений; '
                            'подтверждённой альтернативы из «Связь» с умеренной '
                            'корректировкой найти не удалось.')
    return result


def _improve_catalog_solution(survey,catalog,initial,*,max_rounds=2):
    """Cost-aware local search over EXACT catalogue route combinations.

    Every candidate gets the full mandatory-slab-first estimate, including
    actual fill plate count.  No geometric move is permitted here.
    """
    result=initial
    name='right' if initial.selected_center_position.startswith('right') else 'left'
    by_key={key:variants for key,variants in _groups(survey,catalog)}
    chosen=list(initial.decision_trace['_catalog_hashes'])
    signature_lookup={link_signature(c):c for variants in catalog.values() for c in variants}
    selected=[signature_lookup[s] for s in chosen]
    variants_for=[]
    for i,q in enumerate(selected):
        variants_for.append(by_key.get((q.source,q.medium),[q]))
    evaluated=0
    initial_score=estimate_score(result)
    cache={}
    def run_trial(items):
        nonlocal evaluated
        key=tuple(link_signature(q) for q in items)
        if key in cache:return cache[key]
        try:
            trial=_make_result(survey,name,items)
            # Strict no-invented-path audit even for rejected trial networks.
            audit_catalog_result(trial,catalog)
            evaluated+=1
        except (ValueError,AssertionError):trial=None
        cache[key]=trial
        return trial
    for _ in range(max_rounds):
        gain=False
        order=sorted(range(len(selected)),key=lambda i:(selected[i].source=='fire',
                           selected[i].source.startswith('well:') and any(
                            q.source==selected[i].source for j,q in enumerate(selected) if j!=i)))
        for i in order:
            q=selected[i]
            if q.source=='fire':continue
            others=[item for j,item in enumerate(selected) if j!=i and item.source!='fire']
            alternatives=_improvement_links(variants_for[i],q,others,survey.project,name)
            best_q=q
            best_result=result
            base_score=estimate_score(result)
            for alternative in alternatives:
                if link_signature(alternative)==link_signature(q):continue
                state=selected.copy();state[i]=alternative
                candidate=run_trial(state)
                if candidate is None:continue
                score=estimate_score(candidate)
                if score < base_score-1e-5:
                    base_score=score;best_q=alternative;best_result=candidate
            if best_q is not q:
                selected[i]=best_q;result=best_result;gain=True
        if not gain:break
    result.metrics['catalogue_refinement_networks']=float(evaluated)
    result.metrics['catalogue_before_refinement_cost']=float(initial_score)
    result.metrics['catalogue_refinement_saving']=float(initial_score-estimate_score(result))
    runs=[]
    for i,q in enumerate(selected):
        if q.source=='fire':continue
        for other in selected[i+1:]:
            length=_corridor_overlap(q,other,survey.project)
            if length>=3.0:
                runs.append({'sources':(q.source,other.source),
                             'media':(q.medium,other.medium),
                             'potential_shared_run_m':round(length,2)})
    result.decision_trace['_potential_shared_support_runs']=runs
    if survey.project.shared_corridors:
        result.notes.append('Оптимизация пучков v84: альтернативы взяты только из каталога «Связь»; '
                            'сравнение выполнено по полной стоимости фиксированных и заполняющих плит.')
    return result




def _final_transit_spacing_pass(survey,catalog,result):
    """Late narrow spacing refinement by choosing ONLY catalogued alternatives.

    The primary network and plate supports have already been selected. This
    pass may only swap a link for a close, legal alternative found in Связь;
    it cannot move a tube arbitrarily or change a well's designated port.
    """
    from transit_spacing import spacing_issues, summary
    axial={wp.well.number for wp in survey.wells if wp.well.on_axis}
    issues=spacing_issues(result.segments,axial)
    before=len(issues)
    swaps=0
    if issues:
        lookup={link_signature(q):q for links in catalog.values() for q in links}
        name=result.selected_center_position.split('_')[0]
        chosen=[lookup[key] for key in result.decision_trace['_catalog_hashes']]
        # Only 1 or 2 small moves; this is a correction, not a new routing search.
        for _iteration in range(2):
            best=None
            if not issues:break
            critical={source for entry in issues for source in (entry[7],entry[8])}
            for i,q in enumerate(chosen):
                if _purpose(q) not in critical or q.source=='fire' or q.depends_on:
                    continue
                if any(x.depends_on==route_id(q) for x in chosen):continue
                alternatives=[]
                for candidate in catalog.get(q.source,()):
                    if (candidate is q or candidate.medium!=q.medium or
                        not _available(candidate,name) or candidate.depends_on or
                        candidate.start_side!=q.start_side or
                        abs(candidate.start.u-q.start.u)>1e-5 or
                        abs(candidate.start.v-q.start.v)>1e-5 or
                        abs(candidate.end.u-q.end.u)+abs(candidate.end.v-q.end.v)>.62 or
                        abs(candidate.length-q.length)>1.2 or candidate.elbows>q.elbows):
                        continue
                    # The change must remain confined to a nearby port and the
                    # same general path, not be a different network geometry.
                    if len(candidate.path)!=len(q.path):continue
                    if max(abs(a.u-b.u)+abs(a.v-b.v)
                           for a,b in zip(candidate.path,q.path))>.65:continue
                    alternatives.append(candidate)
                alternatives.sort(key=lambda x:(abs(x.end.u-q.end.u)+abs(x.end.v-q.end.v),
                                                x.length,x.elbows))
                for alternative in alternatives[:8]:
                    trial_links=chosen.copy();trial_links[i]=alternative
                    try:
                        trial=_make_result(survey,name,trial_links)
                        audit_catalog_result(trial,catalog)
                    except (ValueError,AssertionError):continue
                    problem=spacing_issues(trial.segments,axial)
                    if len(problem)>=len(issues):continue
                    # A 0.30 m correction must not reintroduce an avoidable
                    # crossing that the corridor-order pass just eliminated.
                    old_cross=len(_external_link_crossings(chosen,axial))
                    new_cross=len(_external_link_crossings(trial_links,axial))
                    if new_cross>old_cross:continue
                    extra_cost=estimate_score(trial)-estimate_score(result)
                    # Only small corrections: do not buy an arbitrary new
                    # bend/plate row merely to remove a centimetre-scale gap.
                    if extra_cost>survey.project.elbow_cost+survey.project.pipe_cost_per_m*.8:
                        continue
                    rank=(len(problem),round(extra_cost,3),abs(alternative.length-q.length))
                    if best is None or rank<best[0]:best=(rank,trial,trial_links,problem)
            if best is None:break
            _rank,result,chosen,issues=best
            swaps+=1
    result.metrics['transit_spacing_min_m']=.3
    result.metrics['transit_spacing_issues_before']=float(before)
    result.metrics['transit_spacing_issues_after']=float(len(issues))
    result.metrics['transit_spacing_catalogue_swaps']=float(swaps)
    result.decision_trace['_transit_spacing']={
        'required_clear_gap_m':.3,'violations_before':before,
        'violations_after':len(issues),'catalogue_replacements':swaps,
        'unresolved':summary(issues) if issues else ''}
    result.notes.append(summary(issues) if issues else
        f'Межтрубный зазор 0,30 м проверен после маршрутизации: малых замен связей {swaps}.')
    return result


def build_from_catalog(project:Project, survey:Survey|None=None,
                       catalogue:dict[str,list[LinkCandidate]]|None=None)->Result:
    """Select, do not redraw. Validated catalog is the ONLY route authority.

    All source port candidates are generated by generate_links(), the same
    function as the visible 'Связь' tab. Not an exhaustive global optimizer.
    """
    survey=survey or make_survey(project)
    if survey.project is not project:
        # Guard against obsolete or mixed project snapshots.
        from dataclasses import asdict
        if asdict(survey.project)!=asdict(project):
            raise ValueError('Данные проекта изменились после «Связь»; обновите каталог.')
    catalog={k:list(v) for k,v in (catalogue or {}).items()}
    for el in survey.elements:
        if el.status!='inactive' and el.key not in catalog:
            catalog[el.key]=generate_links(survey,el.key)
    groups=_groups(survey,catalog)
    required={key for key,_ in groups}
    # The central plate is chosen from the independent non-fire network.
    # The fire node may choose its nearest legal end only AFTER that decision.
    # A cheaper/shorter fire lead is never allowed to move the central plate.
    evaluated_nonfire=[]
    candidates_by_side={}
    nonfire_failed=defaultdict(int)
    nonfire_checked=0
    for name,plate in survey.plate_variants:
        if project.center_position=='left_auto' and name!='left':
            continue
        if project.center_position not in ('auto','left_auto') and not project.center_position.startswith(name):
            continue
        if project.center_x is not None:
            right=survey.frame.uv(project.center_x,project.center_y).u>max((w.point.u for w in survey.wells if w.well.on_axis),default=0.)
            if (name=='right')!=right:continue
        opts=[]
        for key,variants in groups:
            if key[0]=='fire':
                continue
            pool=_choice_pool([q for q in variants if _available(q,name) and not q.depends_on],project)
            if not pool:
                opts=[];break
            opts.append((key,pool))
        expected_nonfire={key for key,_ in groups if key[0]!='fire'}
        if len(opts)!=len(expected_nonfire):
            continue
        frontier=[(0.,())]
        for group_key,pool in opts:
            candidates=[]
            for rough,chosen in frontier:
                for link in pool:
                    new=(*chosen,link)
                    # A lower-bound *ranking hint* only: count potential
                    # shared physical length and shared slab rows already
                    # present in independent complete links. Full prices are
                    # always recalculated for feasible final configurations.
                    incentive=0.0
                    if project.shared_corridors and link.source!='fire':
                        for previous in chosen:
                            if previous.source=='fire':continue
                            if previous.medium==link.medium and _phase(survey,previous)==_phase(survey,link):
                                incentive+=min(_exact_shared_length(link,previous),12.0)*project.pipe_cost_per_m
                            else:
                                incentive+=min(_corridor_overlap(link,previous,project),12.0)*project.plate_cost/6.0
                    candidates.append((rough+_link_cost(link,project)-min(incentive,48000.0),new))
            candidates.sort(key=lambda item:(item[0],tuple(q.target for q in item[1])))
            frontier=candidates[:BEAM_WIDTH]
            if not frontier:break
        if not frontier and expected_nonfire:continue
        viable=[]
        from time import monotonic
        start_eval=monotonic()
        # Validate promising combinations first. A large project must not
        # fully re-place all support slabs for hundreds of discarded states.
        inspected=0
        for _rough,selected in frontier:
            if viable and (len(viable)>=9 or monotonic()-start_eval>6.0):
                break
            inspected+=1
            if inspected>65 and viable:
                break
            nonfire_checked+=1
            try:
                # Cheap hard constraints before expensive fixed/filler supports.
                problem=_invalid_combo(survey,_network_segs(survey,plate,selected))
                if problem:
                    nonfire_failed[problem[:190]]+=1
                    continue
                result=_make_result(survey,name,selected)
                audit_catalog_result(result,catalog)
            except (ValueError,AssertionError) as exc:
                nonfire_failed[str(exc).split(';')[0][:190]]+=1
                continue
            viable.append((result,selected))
        if viable:
            viable.sort(key=lambda item:(round(estimate_score(item[0]),5),
                                   item[0].metrics.get('topological_elbows',0)))
            # Варианты объединений оцениваются ПОСЛЕ выбора независимых путей.
            # Новых ответвлений к линиям соседних скважин не создаём.
            viable.sort(key=lambda item:(round(estimate_score(item[0]),5),
                                   item[0].metrics.get('topological_elbows',0)))
            # External joint optimizer disabled (off-axis / tie routing removed).
            candidates_by_side[name]=viable[:8]
            evaluated_nonfire.extend((name,run) for run,_ in viable[:8])
    if not candidates_by_side:
        raise ValueError('Нет допустимой сети без пожарного трубопровода для разрешённых '
                         'положений центральной плиты. Новые линии не создаются.')
    # Existing design rule: use the cheapest well + oil tie network to select
    # left/right. On a tie, keep the conventional left-hand layout.
    chosen_side=min(candidates_by_side,key=lambda name:(
        round(estimate_score(candidates_by_side[name][0][0]),5),
        candidates_by_side[name][0][0].metrics.get('topological_elbows',0),
        0 if name=='left' else 1))
    fire_groups=[(key,variants) for key,variants in groups if key[0]=='fire']
    fire_pools=[]
    for key,variants in fire_groups:
        pool=_choice_pool([q for q in variants if _available(q,chosen_side)],project)
        if not pool:
            raise ValueError('Центральная плита зафиксирована по скважинам и точке врезки '
                             f'({chosen_side}). Для пожарного узла нет допустимого подключения '
                             'к её наружному торцу или противоположному торцу хребта. '
                             'Перемещение центральной плиты ради пожарного узла запрещено.')
        fire_pools.append(pool)
    # Evaluate all selected candidate networks at the locked central position.
    # Never re-run the routing algorithm or synthesize any route outside Связь.
    from itertools import product
    evaluated_full=[]
    fire_lengths={}
    fire_failures=defaultdict(int)
    fire_checked=0
    for _base,nonfire in candidates_by_side[chosen_side]:
        for fire_choices in product(*fire_pools):
            selected=(*nonfire,*fire_choices)
            fire_checked+=1
            try:
                result=_make_result(survey,chosen_side,selected)
                audit_catalog_result(result,catalog)
            except (ValueError,AssertionError) as exc:
                fire_failures[str(exc)]+=1
                continue
            evaluated_full.append(result)
            fire_lengths[id(result)]=sum(link.length for link in fire_choices)
    if not evaluated_full:
        reasons=sorted(fire_failures.items(),key=lambda item:(-item[1],item[0]))
        details='; '.join(reason for reason,count in reasons[:2])
        raise ValueError('Положение центральной плиты зафиксировано: '+chosen_side+
                         '. Ни одно пожарное подключение из «Связь» не совместимо '
                         'с остальной сетью. Плиту нельзя переносить ради пожарной линии. '
                         'Причины: '+(details or 'нет допустимой комбинации.'))
    # Rule of the fire node: the nearest FEASIBLE allowed end is chosen
    # within the locked central plate. It cannot decide the plate position.
    chosen=min(evaluated_full,key=lambda r:(round(fire_lengths[id(r)],5),
                           round(estimate_score(r),5),
                           r.metrics.get('topological_elbows',0)))
    selected_fire_length=fire_lengths[id(chosen)]
    # External joint optimizer / transit reordering disabled for core-only mode.
    # chosen=_improve_transit_order(survey,catalog,chosen)
    chosen.decision_trace['_central_selection']={
        'rule':'cheapest_non_fire_network_before_fire_connection',
        'selected':chosen_side,
        'nonfire_cost_by_side':{name:round(estimate_score(items[0][0]),2)
                                for name,items in candidates_by_side.items()},
        'fire_may_change_center':False,
        'fire_connection_length_m':round(selected_fire_length,3),
    }
    scores=chosen.decision_trace['_central_selection']['nonfire_cost_by_side']
    cost_text='; '.join(f'{side}: {cost:,.0f} ₽' for side,cost in sorted(scores.items()))
    chosen.notes.append('Выбор центральной плиты БЕЗ пожарной линии: '+cost_text+
                        '. Решение: '+chosen_side+'. Пожарный узел подключён после фиксации плиты.')
    chosen.metrics['catalogue_evaluated_networks']=float(len(evaluated_full))
    chosen.metrics['catalogue_selected_links']=float(len(chosen.decision_trace['_selected_links']))
    # Независимые линии могут совпадать или делить опоры. Эти случаи
    # определяются по выбранным ГОТОВЫМ путям, а не создают новые пути.
    # Last-stage spacing check, never generate an off-catalogue route.
    chosen=_final_transit_spacing_pass(survey,catalog,chosen)
    # A spacing substitution might change the order again. Audit the final
    # geometry, not the intermediate candidate that was optimised first.
    selected_lookup={link_signature(q):q for rows in catalog.values() for q in rows}
    ordered_final=[selected_lookup[q] for q in chosen.decision_trace['_catalog_hashes']]
    axial_ids={wp.well.number for wp in survey.wells if wp.well.on_axis}
    chosen.metrics['external_transit_crossings']=float(len(_external_link_crossings(ordered_final,axial_ids)))
    # Only annotate crossings: these markers are a rendering layer, not pipe paths.
    from crossing_markers import analyze
    chosen.crossing_markup=analyze(chosen.segments,project.bridge_approach,
          axial_well_ids=[w.well.number for w in chosen.wells if w.well.on_axis])
    chosen.metrics['crossing_marker_count']=float(len(chosen.crossing_markup.markers))
    chosen.metrics['crossing_group_count']=float(chosen.crossing_markup.grouped_count)
    chosen.metrics['crossing_unmarked_count']=float(len(chosen.crossing_markup.unassigned))
    # Final spacing/ordering passes may return freshly constructed Result
    # objects. Preserve the evidence of the ACTUAL joint search performed
    # on the non-fire network before its central plate was fixed.
    phase_result=candidates_by_side[chosen_side][0][0]
    joint_evidence=phase_result.decision_trace.get('_joint_optimizer',{})
    if joint_evidence:
        chosen.decision_trace['_joint_optimizer']={**joint_evidence,
           'phase':'wells_and_product_tie_without_fire',
           'final_cost_including_fire':round(estimate_score(chosen),2)}
        chosen.metrics['joint_nonfire_initial_cost']=float(joint_evidence['initial_cost'])
        chosen.metrics['joint_nonfire_optimized_cost']=float(joint_evidence['best_cost'])
        chosen.metrics['joint_nonfire_saving']=max(0.,float(joint_evidence['initial_cost'])-float(joint_evidence['best_cost']))
        chosen.metrics['joint_evaluated_networks']=float(joint_evidence['full_trials'])
        chosen.notes.append('СОВМЕСТНЫЙ ПОИСК: независимая отправная сеть без пожарного узла '
            f'{joint_evidence["initial_cost"]:,.0f} ₽, сеть после совместных замен '
            f'{joint_evidence["best_cost"]:,.0f} ₽; проверено {joint_evidence["full_trials"]} '
            'полных конфигураций. Оптимум не доказан.')
    audit_catalog_result(chosen,catalog)
    from network_integrity import audit_mandatory_connections
    selected_lookup={link_signature(q):q for choices in catalog.values() for q in choices}
    final_selected=[selected_lookup[sig] for sig in chosen.decision_trace['_catalog_hashes']]
    chosen.decision_trace['_integrity_audit']=audit_mandatory_connections(
        survey,chosen.segments,final_selected,allow_missing_fire=False)
    chosen.metrics['required_connections_checked']=float(chosen.decision_trace['_integrity_audit']['checked'])
    chosen.metrics['required_connections_broken']=0.0
    chosen.decision_trace['_candidate_evaluations']={
        'nonfire_checked':nonfire_checked,
        'nonfire_rejection_reasons':dict(nonfire_failed),
        'fire_checked':fire_checked,
        'fire_rejection_reasons':dict(fire_failures),
        'complete_valid_networks':len(evaluated_full),
    }
    return chosen
