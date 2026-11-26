"""Предварительная сметная стоимость маршрутов для v75.

Суммируем физические метры труб, фактические отводы и геометрически
размещённые плиты 1,2 x 3 м. Нет условной скидки за близость линий.
Окончательная стоимость может меняться при добавлении конструктивных опор.
"""
from collections import defaultdict
from math import hypot

TRUNK_PURPOSES = ('Хребет','Перспективный хребет','Перспективное продление хребта')


def _key(p):return (round(p.u,6),round(p.v,6))


def network_fittings(segments):
    """Число уникальных топологических тройников и поворотов.

    Ветвь, пришедшая в ЗАКРЫТЫЙ конец магистрали хребта, — тройник с
    заглушенным продолжением, а не колено (заглушка без лишней длины).
    """
    from pipe_render import make_network
    graph=defaultdict(lambda: defaultdict(set))
    for med,edges in make_network(segments).items():
        for a,b,_phase in edges:
            graph[med][a].add(b);graph[med][b].add(a)
    trunk_endpoint=set()
    for s in segments:
        if s.purpose in TRUNK_PURPOSES:
            trunk_endpoint.add((s.medium,_key(s.a)))
            trunk_endpoint.add((s.medium,_key(s.b)))
    tees=0;elbows=0;terminal_tees=[];fourways=[]
    for medium,nodes in graph.items():
        for node,neighbours in nodes.items():
            if len(neighbours)>3:
                fourways.append((medium,node,len(neighbours)))
                continue
            if len(neighbours)==3:
                tees+=1
                continue
            if len(neighbours)!=2:continue
            d=[(p[0]-node[0],p[1]-node[1]) for p in neighbours]
            if abs(d[0][0]*d[1][0]+d[0][1]*d[1][1])>1e-8:continue
            # Если из окончания магистрального участка уходит боковая
            # ветвь, оставшийся магистральный раструб должен быть закрыт.
            if (medium,node) in trunk_endpoint:
                tees+=1;terminal_tees.append((medium,node))
            else:
                elbows+=1
    return {'tees':tees,'elbows':elbows,'terminal_tees':terminal_tees,'fourways':fourways}


def transit_corridor_overlap(segments, gap):
    """Условные метры совместной прокладки транзитов без подсчёта МИС.

    Сравниваем только ПАРАЛЛЕЛЬНЫЕ независимые участки разных сред,
    похожие на транзитные линии; хребет, плиту и осевые отпайки исключаем.

    Межосевое 0,30 м на плите 1,2 м — критерий пучка (не lane_gap хребта).
    Аргумент gap сохранён для совместимости вызовов/JSON, на отбор не влияет.
    """
    from routing import PIPE_OD
    from bundle_geometry import AXIS_GAP, TRANSIT_WIDTH
    _=gap  # kept for call-site compatibility; transit packs use AXIS_GAP
    candidates=[]
    for s in segments:
        purpose=s.purpose.casefold()
        # Независимый пожарный вывод физически может лежать рядом с трубой
        # ДРУГОЙ среды, но он не является транзитной магистралью скважин.
        # Приписывать такой паре экономию транзитного пучка нельзя:
        # иначе скважинная СППВ искусственно притягивается к пожарной трассе.
        if 'пожарного узла' in purpose or 'резерв пожарного вывода' in purpose:
            continue
        # Do NOT drop «Отвод от скв.»: off-axis transit uses that purpose too.
        # Structural spine / plate pipes are excluded; well and tie routes remain.
        if any(word in purpose for word in ('хребет','плита:','к существующему хребту')):
            continue
        if purpose.startswith('центральная плита'):
            continue
        if ('скв.' not in purpose and 'врезке' not in purpose and 'коридор' not in purpose
                and 'общая нитка' not in purpose and 'транзит' not in purpose):
            continue
        if s.length<1.0:continue
        axis=s.a.v if s.horizontal else s.a.u
        lo,hi=sorted((s.a.u,s.b.u) if s.horizontal else (s.a.v,s.b.v))
        candidates.append((s.horizontal,axis,lo,hi,s.medium))
    # При трёх нитках не суммируем одну общую полосу повторно за каждую пару.
    intervals=[]
    for i,a in enumerate(candidates):
        for b in candidates[i+1:]:
            if a[0]!=b[0] or a[4]==b[4]:continue
            distance=abs(a[1]-b[1])
            maximum=TRANSIT_WIDTH-(PIPE_OD[a[4]]+PIPE_OD[b[4]])/2
            if not AXIS_GAP-1e-6<=distance<=maximum+1e-6:continue
            lo=max(a[2],b[2]);hi=min(a[3],b[3])
            if hi-lo>0.01:intervals.append((a[0],round((a[1]+b[1])/2,1),lo,hi))
    total=0.0
    for key in set((v[0],v[1]) for v in intervals):
        ranges=sorted((v[2],v[3]) for v in intervals if v[:2]==key)
        left=right=None
        for lo,hi in ranges:
            if left is None:left,right=lo,hi
            elif lo<=right:right=max(right,hi)
            else:total+=right-left;left,right=lo,hi
        if left is not None:total+=right-left
    return total


def count_small_plates(slabs):
    """Количество прямоугольных 1,2x3 м плит вне зависимости от ориентации.

    Не тарифицируем по цене малой плиты 6x1,75, 3x1,75 и центральную плиту:
    пользователь пока не назначил им тарифы.
    """
    seen=set()
    for slab in slabs:
        sides=sorted((abs(slab.length), abs(slab.height)))
        if abs(sides[0]-1.2)>1e-4 or abs(sides[1]-3.0)>1e-4:
            continue
        # Dedup boxes equal coordinates/geometry; duplicates cannot yield savings.
        seen.add((round(slab.u_start,4),round(slab.u_end,4),
                  round(slab.v_min,4),round(slab.v_max,4)))
    return len(seen)


def cost_components(result):
    p=result.project;m=result.metrics
    return {
      'pipe':p.pipe_cost_per_m*m['pipe_m'],
      'elbows':p.elbow_cost*float(m.get('topological_elbows',m.get('bends',0))),
      'tees':p.tee_cost*float(m.get('topological_tees',0)),
      'small_plates':p.plate_cost*count_small_plates(result.slabs),
      'crossings':0.0,  # v74: 2D-маркеры пересечений, без сметного штрафа
      'corridor_saving':0.0,  # Ни скидок за совпадение, ни оплаты общего коридора

      'geometry_penalty':10000*float(m.get('overlaps',0))+100000*float(m.get('foreign_contacts',0))
                         +1e6*float(m.get('fourway_count',0)),
    }


def estimate_score(result):
    c=cost_components(result)
    return c['pipe']+c['elbows']+c['tees']+c['small_plates']+c['crossings']+c['geometry_penalty']


def axial_branch_conflicts(segments, axial_well_ids):
    """Подозрительные пересечения осевых отпайков с НЕхребтовыми линиями.

    Пересечения осевых отпайков с хребтом или с центральной плитой разрешены,
    их верхней нитью всегда назначается осевой отпаек.
    """
    from routing import intersect
    ids={int(n) for n in axial_well_ids}
    branches=[]
    for s in segments:
        if not s.purpose.startswith('Отвод от скв. '):continue
        try:num=int(s.purpose.split('скв. ',1)[1].split()[0])
        except Exception:continue
        if num in ids:branches.append((num,s))
    problems=set()
    for number,a in branches:
        for b in segments:
            if a is b or a.medium==b.medium:continue
            typ,pt=intersect(a,b)
            if typ!='cross':continue
            # Осевой отпаек вправе проходить ПОВЕРХ хребта/плиты.
            # Это допустимый условный переход, а не конфликт компоновки.
            if b.purpose in TRUNK_PURPOSES or b.purpose=='Центральная плита: магистральная труба':
                continue
            key=(number,round(pt.u,3),round(pt.v,3),tuple(sorted((a.medium,b.medium))))
            problems.add(key)
    return sorted(problems)
