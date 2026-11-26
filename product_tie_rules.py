"""Единые геометрические ограничения для врезки и внеосевых скважин.

Места на хребте возможны ТОЛЬКО на плитах; шаг сетки 0,1 м.
На центральной плите вход допустим только с продольной стороны.
Исключение — свободный удалённый торец ХРЕБТА, противоположный центру.
"""
from __future__ import annotations
from dataclasses import dataclass
import math

STEP = .1
END_MARGIN = .35  # конструктивное предварительное отступление от границы плиты
BRANCH_CLEARANCE = .45  # предварительная охранная полоса у скважинного тройника
TRUNK_KINDS = frozenset(('Хребет','Перспективный хребет','Перспективное продление хребта'))
SUPPORT_KINDS = frozenset(('central','axial_junction','trunk_intermediate','trunk_tap_plate'))


@dataclass(frozen=True)
class TieSite:
    point: object
    kind: str  # side: боковой вход, end: только свободный торец хребта
    plate_type: str
    u_min: float
    u_max: float
    v_min: float
    v_max: float


def tick_values(low, high, *, step=STEP):
    """Каждые 0.1 м по абсолютной координате U; без накопления ошибки float."""
    first=math.ceil((low-1e-8)/step)
    last=math.floor((high+1e-8)/step)
    return [round(k*step,6) for k in range(first,last+1)]


def product_trunk_segments(segments):
    return medium_trunk_segments(segments, 'oil_product')


def medium_trunk_segments(segments, medium):
    """The same support requirement applies to every off-axis well medium."""
    return [s for s in segments if s.medium==medium and s.purpose in TRUNK_KINDS
            and abs(s.a.v-s.b.v)<1e-7]


def site_conflicts_with_well(u, occupied):
    return any(abs(u-b)<BRANCH_CLEARANCE-1e-7 for b in occupied)


def enumerate_tie_sites(trunk, slabs, central_plate, occupied=(), *,
                        include_far_end=True, required_phase=None, medium="oil_product"):
    """Все разрешённые дискретные точки. Не требует полноценной трассировки.

    slabs: реальные опорные плиты или геометрические прогнозы для режима Связь.
    occupied: координаты U существующих осевых скважинных присоединений.
    """
    from routing import Pt
    spans=[seg for seg in medium_trunk_segments(trunk, medium)
           if required_phase!='active' or seg.phase=='active']
    if not spans:
        return []
    lane=spans[0].a.v
    result={}
    for slab in slabs:
        if slab.slab_type not in SUPPORT_KINDS or medium not in slab.media:
            continue
        if required_phase=='active' and slab.phase=='future':
            continue
        if slab.slab_type=='central':
            # центральная плита не относится к хребту; отдельно учитывается
            # её реально существующий внутренний продуктовый участок.
            continue
        for seg in spans:
            if abs(seg.a.v-lane)>1e-5:continue
            a,b=sorted((seg.a.u,seg.b.u))
            low=max(a,slab.u_start+END_MARGIN)
            high=min(b,slab.u_end-END_MARGIN)
            for u in tick_values(low,high):
                if site_conflicts_with_well(u,occupied):continue
                result[('side',u)]=TieSite(Pt(u,lane),'side',slab.slab_type,
                    slab.u_start,slab.u_end,slab.v_min,slab.v_max)
    if include_far_end:
        all_u=[v for s in spans for v in (s.a.u,s.b.u)]
        # Вытягиваем только ДАЛЬНИЙ торец, противоположный центральной плите.
        left=central_plate.trunk_face_u <= (min(all_u)+max(all_u))/2
        u=max(all_u) if left else min(all_u)
        result[('end',round(u,6))]=TieSite(Pt(u,lane),'end','free_trunk_end',
            u,u,lane,lane)
    return sorted(result.values(),key=lambda s:(s.point.u,0 if s.kind=='side' else 1))


def central_side_sites(plate, lanes, medium="oil_product"):
    """Все точки на продуктовой нитке плиты с шагом 0.1 м, без торцов."""
    from routing import Pt
    return [TieSite(Pt(u,lanes[medium]),'side','central',
                    plate.u_min,plate.u_max,plate.v_min,plate.v_max)
            for u in tick_values(plate.u_min+END_MARGIN,plate.u_max-END_MARGIN)]


def object_on_trunk_external_side(source_v, lane_v, trunk_side):
    """A source on the NDS-facing side cannot use a lateral trunk tap.

    Axial wells are intentionally handled by their own fixed-branch rule;
    the opposite FREE end remains an independently allowed exception.
    """
    return trunk_side in (-1,1) and (source_v-lane_v)*trunk_side>1e-6


def candidate_gates(site, *, trunk_side, exterior=.35, source_v=None):
    """Внешний ввод к хребту; у центральной плиты боковые грани обе допустимы.

    Внешняя сторона определяется знаком V хребта относительно оси НДС,
    а не местоположением точки врезки. Шаг портов остаётся равным 0,1 м.
    """
    from routing import Pt
    if site.kind=='end':
        return []
    if site.plate_type=='central':
        if source_v is None:
            # Compatibility with existing callers; new routing ALWAYS supplies
            # the actual source V to enforce the corresponding side only.
            return [Pt(site.point.u,site.v_min-exterior),
                    Pt(site.point.u,site.v_max+exterior)]
        edge=(site.v_max if source_v>= (site.v_min+site.v_max)/2 else site.v_min)
        return [Pt(site.point.u,edge+exterior*(1 if edge==site.v_max else -1))]
    if trunk_side not in (-1,1):
        raise ValueError('Для бокового подключения требуется сторона хребта')
    v=(site.v_max+exterior if trunk_side==1 else site.v_min-exterior)
    return [Pt(site.point.u,v)]


def end_outward_sign(central_plate, end_point):
    """Направление от хребта прочь от его центральной плиты."""
    return 1 if end_point.u>=central_plate.trunk_face_u else -1


def preview_support_slabs(survey,plate):
    """Предварительные геометрические плиты, НЕ устанавливаемые в проект.

    Привязки 3-м осевых площадок и промежуточных плит определяются
    исключительно по уже известным координатам осевых скважин.
    Окончательное подтверждение выполняется после строительства сети.
    """
    from routing import Slab
    from axial_plates import create_intermediate_trunk_plates
    spans=product_trunk_segments(survey.trunk_segments)
    if not spans:return []
    used=tuple(sorted({s.medium for s in survey.trunk_segments}))
    central=Slab(plate.u_min,plate.u_max,plate.v_min,plate.v_max,
                 'active',len(used),used,'central')
    supports=[central]
    for wp in survey.wells:
        if not wp.well.on_axis or wp.well.status=='inactive' or not wp.connections:
            continue
        lo=wp.point.u-1.5;hi=wp.point.u+1.5
        if min(hi,plate.u_max)-max(lo,plate.u_min)>1e-6:
            continue
        supports.append(Slab(lo,hi,plate.v_min,plate.v_max,
            wp.well.status,len(used),used,'axial_junction'))
    supports.sort(key=lambda q:(q.u_start,q.u_end))
    supports += create_intermediate_trunk_plates(supports,survey.trunk_segments,
                                                  survey.lanes,plate)
    return supports


def projected_trunk_sites(placements, central_plate, lanes, medium,
                          head_u, reach, phase):
    """Common preliminary support-limited connection sites for build/groups.

    This does not install slabs. The final slab stage must confirm the
    attachment; unresolved conflicts remain errors, never silent end taps.
    """
    from routing import Pt, Segment, Slab
    from axial_plates import create_intermediate_trunk_plates
    if reach <= head_u+1e-6 or central_plate is None:
        return []
    mock=[Segment(Pt(head_u,lanes[medium]),Pt(reach,lanes[medium]),
                  medium,phase,'Хребет')]
    supports=[Slab(central_plate.u_min,central_plate.u_max,
                   central_plate.v_min,central_plate.v_max,
                   phase,1,(medium,),'central')]
    occupied=[]
    for axial in placements:
        if not axial.well.on_axis or axial.well.status=='inactive':continue
        kinds={m for m,_ in axial.connections}
        if not kinds:continue
        u=axial.point.u
        if medium in kinds:occupied.append(u)
        if u+1.5 < head_u or u-1.5 > reach:continue
        supports.append(Slab(u-1.5,u+1.5,central_plate.v_min,
                central_plate.v_max,axial.well.status,1,(medium,),
                'axial_junction'))
    supports+=create_intermediate_trunk_plates(
             supports,mock,lanes,central_plate)
    return enumerate_tie_sites(mock,supports,central_plate,occupied,
                        required_phase=phase,medium=medium)


def product_axial_occupied(survey):
    """Примерное положение скважинных тройников до расчёта сети."""
    from routing import well_medium_outlets, well_connection_normal, WELL_MIN_STRAIGHT
    values=[]
    for wp in survey.wells:
        if not wp.well.on_axis or wp.well.status=='inactive':continue
        outlets=well_medium_outlets(wp,survey.trunk_side,survey.center,
                                    survey.project.lane_gap)
        if not outlets:continue
        normal=well_connection_normal(wp,survey.trunk_side,survey.center)
        for outlet in outlets.values():
            values.append(outlet.u+normal.u*WELL_MIN_STRAIGHT)
    return values
