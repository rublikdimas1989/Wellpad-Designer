"""Опора пересечений, тройников и отводов на плитах.

Расчёт без изменения маршрутов: ставим полноразмерные плиты 1,2 × 3 м
там, где узел не находится на существующей плите или в 0,25 м от неё.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import hypot

from routing import Pt, Slab, PIPE_OD, EPS
from pipe_render import make_network
from crossing_markers import runs_from_segments, find_crossings

CLEARANCE = 0.25
LENGTH = 3.0
WIDTH = 1.2
TYPE = 'fitting_support'


@dataclass(frozen=True)
class FittingSite:
    kind: str
    point: Pt
    media: tuple[str, ...]


def support_sites(segments):
    """Фактические узлы из сетевого графа и геометрические пересечения разных сред.

    Обычный конец прямого отрезка не является фасонным элементом.
    Исключение: угловое окончание хребта конструктивно является тройником.
    """
    trunk={'Хребет','Перспективный хребет','Перспективное продление хребта'}
    trunk_ends={(s.medium,round(q.u,6),round(q.v,6))
                for s in segments if s.purpose in trunk for q in (s.a,s.b)}
    sites={}
    priority={'crossing':3,'tee':2,'elbow':1}
    for medium,edges in make_network(segments).items():
        g=defaultdict(set)
        for a,b,phase in edges:
            g[a].add(b);g[b].add(a)
        for (u,v),nbrs in g.items():
            kind=None
            if len(nbrs)>=3:
                kind='tee'
            elif len(nbrs)==2:
                a,b=tuple(nbrs)
                da=(a[0]-u,a[1]-v)
                db=(b[0]-u,b[1]-v)
                if abs(da[0]*db[0]+da[1]*db[1])<1e-8:
                    kind='tee' if (medium,round(u,6),round(v,6)) in trunk_ends else 'elbow'
            if kind:
                key=(round(u,5),round(v,5))
                old=sites.get(key)
                if old is None:
                    sites[key]=FittingSite(kind,Pt(u,v),(medium,))
                else:
                    sites[key]=FittingSite(old.kind if priority[old.kind]>=priority[kind] else kind,
                                           old.point,tuple(sorted(set(old.media+(medium,)))))
    runs=runs_from_segments(segments)
    for c in find_crossings(runs):
        pt=c.point
        key=(round(pt.u,5),round(pt.v,5))
        media=(runs[c.a].medium,runs[c.b].medium)
        old=sites.get(key)
        sites[key]=FittingSite('crossing',pt,tuple(sorted(set((old.media if old else ()) + media))))
    return sorted(sites.values(), key=lambda s:(s.point.u,s.point.v,s.kind))


def slab_distance(point, slab):
    du=max(0.,slab.u_start-point.u,point.u-slab.u_end)
    dv=max(0.,slab.v_min-point.v,point.v-slab.v_max)
    return hypot(du,dv)


def unsupported_sites(sites, slabs, tolerance=CLEARANCE):
    return [s for s in sites if not any(slab_distance(s.point,pl)<=tolerance+1e-6 for pl in slabs)]


def _positive_overlap(a,b):
    return min(a.u_end,b.u_end)-max(a.u_start,b.u_start)>1e-6 and min(a.v_max,b.v_max)-max(a.v_min,b.v_min)>1e-6


def _intrudes_trunk(slab, segments):
    """A small transit/support plate must not cover the main trunk band.

    Trunk tees/crossings are handled by separate 3 × 1.75 m connecting
    slabs; do not disguise such a slab as 1.2 × 3 m transit support.
    """
    trunk={'Хребет','Перспективный хребет','Перспективное продление хребта'}
    return any(s.purpose in trunk and s.horizontal and
               slab.v_min+1e-5 < s.a.v < slab.v_max-1e-5 and
               min(slab.u_end,max(s.a.u,s.b.u))-
               max(slab.u_start,min(s.a.u,s.b.u))>.05
               for s in segments)


def _nearby_pipe_segments(site, segments):
    x,y=site.point.u,site.point.v
    for s in segments:
        if s.length < .01:
            continue
        if s.horizontal:
            dist=abs(y-s.a.v)
            lo,hi=sorted((s.a.u,s.b.u))
            along=x
        else:
            dist=abs(x-s.a.u)
            lo,hi=sorted((s.a.v,s.b.v))
            along=y
        if dist <= WIDTH/2+.15 and lo-.25 <= along <= hi+.25:
            yield s


def _candidate_slabs(site, segments, existing_slabs):
    # Поперечная ось ориентирована по отдельной трубе, но может вместить пучок.
    candidates=[]
    positions=(-1.75,-1.5,-1.,-.5,0.,.5,1.,1.5,1.75)
    for s in _nearby_pipe_segments(site,segments):
        ori='h' if s.horizontal else 'v'
        fixed=s.a.v if s.horizontal else s.a.u
        along=site.point.u if s.horizontal else site.point.v
        lo,hi=sorted((s.a.u,s.b.u) if s.horizontal else (s.a.v,s.b.v))
        # При плотном боковом подводе к центральной плите обычная
        # центрированная плита перекрывает её край. Попробуем поставить
        # опорную плиту ВПЛОТНУЮ к существующей боковой грани.
        cross_centers={fixed}
        for old in existing_slabs:
            bounds=(old.v_min,old.v_max) if ori=='h' else (old.u_start,old.u_end)
            for edge,side in ((bounds[0],-1),(bounds[1],1)):
                test_center=edge + side*WIDTH/2
                if abs(test_center-fixed)<=WIDTH/2-PIPE_OD.get(s.medium,.1)/2+1e-6:
                    cross_centers.add(round(test_center,6))
        for delta in positions:
            center=along+delta
            start,end=center-LENGTH/2,center+LENGTH/2
            overlap=max(0.,min(hi,end)-max(lo,start))
            if overlap < 1.5:
                continue
            for cross_center in cross_centers:
                if ori=='h':
                    slab=Slab(start,end,cross_center-WIDTH/2,cross_center+WIDTH/2,
                              s.phase,1,(s.medium,),TYPE)
                else:
                    slab=Slab(cross_center-WIDTH/2,cross_center+WIDTH/2,start,end,
                              s.phase,1,(s.medium,),TYPE)
                if slab_distance(site.point,slab)>CLEARANCE+1e-6:
                    continue
                if _intrudes_trunk(slab,segments):
                    continue
                candidates.append((slab,overlap))
    # Deterministic duplicate removal.
    unique={}
    for slab,overlap in candidates:
        k=(round(slab.u_start,5),round(slab.u_end,5),round(slab.v_min,5),round(slab.v_max,5))
        if k not in unique or unique[k][1]<overlap:
            unique[k]=(slab,overlap)
    return list(unique.values())


def _framework_alignment_candidates(site, segments, fixed, candidates):
    """Snap a bend support to the physical edge guides of nearby fixed slabs.

    No pipe is displaced. All proposed alternatives still contain the actual
    elbow, support at least 1.5 m of a participating straight pipe, and must
    pass ordinary slab-overlap / trunk checks. Aligning TWO compatible edges
    is preferred, then the edge of the slab carrying more pipe media.
    """
    from routing import Slab
    extended={}
    for slab,old_overlap in candidates:
        ori='v' if slab.height>slab.length else 'h'
        uu=[slab.u_start];vv=[slab.v_min]
        for guide in fixed:
            if guide.slab_type=='central':continue
            if abs(guide.u_start-site.point.u)>140 and abs(guide.u_end-site.point.u)>140:
                continue
            if abs(guide.v_min-site.point.v)>140 and abs(guide.v_max-site.point.v)>140:
                continue
            # Horizontal rows guide the top/bottom edge of an elbow slab;
            # vertical rows guide the right/left edge.
            if guide.length>=guide.height and abs((guide.v_min+guide.v_max)/2-site.point.v)<1.0:
                for edge in (guide.v_min,guide.v_max):
                    for option in (edge,edge-slab.height):
                        if abs(option-slab.v_min)<=2.0:vv.append(option)
            if guide.height>guide.length and abs((guide.u_start+guide.u_end)/2-site.point.u)<1.0:
                for edge in (guide.u_start,guide.u_end):
                    for option in (edge,edge-slab.length):
                        if abs(option-slab.u_start)<=2.0:uu.append(option)
        for u0 in set(round(x,6) for x in uu):
            for v0 in set(round(y,6) for y in vv):
                candidate=Slab(u0,u0+slab.length,v0,v0+slab.height,
                               slab.phase,slab.media_count,slab.media,slab.slab_type)
                if slab_distance(site.point,candidate)>1e-6 or _intrudes_trunk(candidate,segments):
                    continue
                if any(_positive_overlap(candidate,s) for s in fixed):continue
                coverage=0.
                for s in _nearby_pipe_segments(site,segments):
                    if s.horizontal != (ori=='h'):continue
                    cr=(candidate.v_min,candidate.v_max) if s.horizontal else (candidate.u_start,candidate.u_end)
                    val=s.a.v if s.horizontal else s.a.u
                    if not cr[0]+.02 <= val <= cr[1]-.02:continue
                    br=sorted((s.a.u,s.b.u) if s.horizontal else (s.a.v,s.b.v))
                    sr=(candidate.u_start,candidate.u_end) if s.horizontal else (candidate.v_min,candidate.v_max)
                    coverage=max(coverage,max(0.,min(br[1],sr[1])-max(br[0],sr[0])))
                if coverage<1.5-1e-6:continue
                # Score physically meaningful matches with support guides
                # sharing the local horizontal or vertical pipe corridor.
                axes=set();weighted=0
                for g in fixed:
                    if g.slab_type=='central':continue
                    if (g.length>=g.height and abs((g.v_min+g.v_max)/2-site.point.v)<1.
                        and (abs(candidate.v_min-g.v_min)<1e-5 or abs(candidate.v_max-g.v_max)<1e-5)):
                        axes.add('v');weighted+=max(1,g.media_count)
                    if (g.height>g.length and abs((g.u_start+g.u_end)/2-site.point.u)<1.
                        and (abs(candidate.u_start-g.u_start)<1e-5 or abs(candidate.u_end-g.u_end)<1e-5)):
                        axes.add('u');weighted+=max(1,g.media_count)
                key=(round(u0,5),round(v0,5),round(candidate.u_end,5),round(candidate.v_max,5))
                score=(len(axes),weighted,coverage,-(abs(u0-slab.u_start)+abs(v0-slab.v_min)))
                if key not in extended or score>extended[key][0]:extended[key]=(score,candidate)
    return sorted(extended.values(),key=lambda q:q[0],reverse=True)


def _shift_nearest_transit(site, transit_slabs, fixed_slabs, segments):
    """Сдвинуть существующую регулярную плиту к углу вдоль своей линии.

    Угловая плита не должна без причины торчать за поворот: сначала
    рассматриваем положение, где одна её короткая грань у поворота,
    затем вариант с поворотом в середине и лишь после — прочие.
    """
    candidates=[]
    for old in transit_slabs:
        horizontal=old.length > old.height
        x,y=site.point.u,site.point.v
        cross=(old.v_min+old.v_max)/2 if horizontal else (old.u_start+old.u_end)/2
        if abs((y if horizontal else x)-cross)>WIDTH/2+CLEARANCE:
            continue
        along=x if horizontal else y
        old_mid=((old.u_start+old.u_end)/2 if horizontal
                 else (old.v_min+old.v_max)/2)
        # Установку начинаем с вариантов, полностью лежащих на подходящей
        # прямой трубе; не назначаем одно колено центром двух соседних плит.
        own_lines=[]
        for seg in segments:
            if (seg.horizontal != horizontal or seg.medium not in old.media):
                continue
            pipe_cross=seg.a.v if horizontal else seg.a.u
            if abs(pipe_cross-cross)>WIDTH/2:
                continue
            lo,hi=sorted((seg.a.u,seg.b.u) if horizontal else (seg.a.v,seg.b.v))
            if lo-CLEARANCE <= along <= hi+CLEARANCE:
                own_lines.append((lo,hi))
        for target in (along-LENGTH/2, along+LENGTH/2, along,
                       along-LENGTH/2+CLEARANCE,along+LENGTH/2-CLEARANCE):
            displacement=target-old_mid
            if abs(displacement)>3.1:
                continue
            if horizontal:
                candidate=Slab(old.u_start+displacement,old.u_end+displacement,
                               old.v_min,old.v_max,old.phase,old.media_count,old.media,TYPE)
            else:
                candidate=Slab(old.u_start,old.u_end,old.v_min+displacement,
                               old.v_max+displacement,old.phase,old.media_count,old.media,TYPE)
            if slab_distance(site.point,candidate)>CLEARANCE+1e-6:
                continue
            if any(_positive_overlap(candidate,fixed) for fixed in fixed_slabs):
                continue
            if _intrudes_trunk(candidate,segments):
                continue
            slab_lo,slab_hi=((candidate.u_start,candidate.u_end) if horizontal
                             else (candidate.v_min,candidate.v_max))
            overlap=max((max(0,min(slab_hi,hi)-max(slab_lo,lo))
                         for lo,hi in own_lines),default=0.0)
            if overlap < 1.5:
                continue
            # Сначала наибольшее покрытие реальной трубы, затем минимальный
            # сдвиг существующей плиты. Это убирает лишние выступы за угол.
            candidates.append((-overlap,abs(displacement),candidate,old))
    if not candidates:
        return None
    _overlap,dist,candidate,old=min(candidates,key=lambda t:(t[0],t[1]))
    return dist,candidate,old


def complete_support(segments, initial_slabs, strict=True, exact_sites=(), *, offaxis_well_ids=()):
    """Двухэтапная раскладка опор.

    1. Сначала фиксируются центральные/специальные плиты и плиты всех
       отводов, тройников и пересечений. Ближайшая регулярная плита
       сдвигается только ВДОЛЬ своей линии, если такой сдвиг допустим.
    2. Все оставшиеся обычные плиты пересчитываются ЗАНОВО на промежутках
       между зафиксированными плитами. Требование 2..4 м относится к
       регулярным сериям, а правило поддержки узла имеет приоритет.
    """
    from axial_plates import create_transit_run_plates

    # Exact external elbows must be ON a slab, not just 0.25 m from its edge.
    exact_sites={(round(x,5),round(y,5)) for x,y in exact_sites}
    sites=support_sites(segments)
    existing={(round(s.point.u,5),round(s.point.v,5)) for s in sites}
    for u,v in sorted(exact_sites-existing):
        sites.append(FittingSite('elbow',Pt(u,v),()))
    def uncovered(site,plates):
        key=(round(site.point.u,5),round(site.point.v,5))
        tolerance=1e-6 if key in exact_sites else CLEARANCE
        return not any(slab_distance(site.point,pl)<=tolerance+1e-6 for pl in plates)
    originals=[s for s in initial_slabs if s.slab_type=='transit_run']
    fixed=[s for s in initial_slabs if s.slab_type!='transit_run']
    anchored=[]
    moved=[]
    consumed=set()

    # Фиксируем существующие плиты, на которые приходится фасонный узел.
    for site in sites:
        if not uncovered(site,fixed):
            continue
        matching=[(slab_distance(site.point,old),i,old)
                  for i,old in enumerate(originals)
                  if i not in consumed and slab_distance(site.point,old)<=CLEARANCE+1e-6]
        if matching:
            _,i,old=min(matching,key=lambda t:(t[0],t[1]))
            anchored.append(Slab(old.u_start,old.u_end,old.v_min,old.v_max,
                                  old.phase,old.media_count,old.media,TYPE))
            consumed.add(i)
            fixed.append(anchored[-1])

    # Не защищённые опорой узлы: сдвигаем ближайшую плиту, не меняя коридор,
    # и только при невозможности сдвига ставим новую опору.
    pending=[site for site in sites if uncovered(site,fixed)]
    # Узлы с наименьшим числом возможных мест решаем первыми: иначе
    # свободный узел может занять единственную опору у грани плиты.
    pending.sort(key=lambda site:(
        sum(not any(_positive_overlap(candidate,old) for old in fixed)
            for candidate,_ in _candidate_slabs(site,segments,fixed)),
        site.point.u,site.point.v))
    for site in pending:
        if not uncovered(site,fixed):
            continue
        free=[(i,old) for i,old in enumerate(originals) if i not in consumed]
        shifted=_shift_nearest_transit(site,[v for i,v in free],fixed,segments)
        if shifted is not None:
            _,candidate,old=shifted
            chosen_i=next(i for i,v in free if v is old)
            fixed.append(candidate)
            anchored.append(candidate)
            moved.append(old)
            consumed.add(chosen_i)
            continue
        candidates=_candidate_slabs(site,segments,fixed)
        aligned=_framework_alignment_candidates(site,segments,fixed,candidates)
        feasible=[(score,slab) for score,slab in aligned
                  if (not any(_positive_overlap(slab,f) for f in fixed)
                      and not uncovered(site,[slab]))]
        if feasible:
            _,slab=max(feasible,key=lambda x:x[0])
            fixed.append(slab)
            anchored.append(slab)

    # Важный шаг: старые транзитные плиты больше не используются как
    # препятствия. Свободные участки заполняем новой РАВНОМЕРНОЙ серией.
    regular=create_transit_run_plates(segments,fixed,offaxis_well_ids=offaxis_well_ids)
    final=[*fixed,*regular]
    missing=[site for site in sites if uncovered(site,final)]
    if missing and strict:
        sample='; '.join(f'{s.kind} ({s.point.u:.2f}; {s.point.v:.2f})' for s in missing[:5])
        raise ValueError('Нельзя обеспечить опору для фасонных узлов после фиксирования '
                         'угловых плит и перераскладки промежутков. '
                         f'Неподдержанных элементов: {len(missing)}. {sample}. '
                         'Нужно изменить трассу или тип плит.')
    # Вызывающий код удаляет ВСЮ прежнюю регулярную серию, затем
    # добавляет зафиксированные опоры и новый равномерный ряд.
    removed=list(originals)
    added=[*anchored,*regular]
    return added,removed,sites,missing
