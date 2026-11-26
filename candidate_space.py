"""Пространство геометрических начальных и конечных портов для диагностики.

Непрерывный отрезок допускает бесконечно много координат. Поэтому отчёт
разделяет фактически оценённые точки и дополнительные геометрические точки,
порождаемые правилами. Не выдаём второй класс за отвергнутый оптимизатором.
"""
from __future__ import annotations
from dataclasses import replace
import re
from routing import Pt, MEDIA, PIPE_OD, well_medium_outlets, SIDE_ONLY_PLATE_MEDIA


def _key(point):
    return (round(point.u,5),round(point.v,5))


def _owner(purpose):
    match=re.search(r'скв\.\s*(\d+)(?!\d)',purpose,re.I)
    return int(match.group(1)) if match else None


def _near_seg(seg, point):
    if seg.horizontal:
        return min(seg.a.u,seg.b.u)-1e-6<=point.u<=max(seg.a.u,seg.b.u)+1e-6 and abs(point.v-seg.a.v)<1e-5
    return min(seg.a.v,seg.b.v)-1e-6<=point.v<=max(seg.a.v,seg.b.v)+1e-6 and abs(point.u-seg.a.u)<1e-5


def candidate_points(result,index):
    segment=result.segments[index]
    medium=segment.medium
    owner=_owner(segment.purpose)
    source=[]
    def add_start(q,title,used=False,status=None):
        if _key(q) not in {_key(e['point']) for e in source}:
            source.append({'point':q,'label':title,'status':status or ('выбран' if used else 'возможен')})

    if owner is not None:
        wp=next((w for w in result.wells if w.well.number==owner),None)
        if wp is not None:
            for direc in ('u+','u-','v+','v-'):
                ori='perpendicular' if direc[0]=='u' else 'parallel'
                variant=replace(wp,well=replace(wp.well,exit_direction=direc),orientation=ori)
                outlets=well_medium_outlets(variant,result.trunk_side,result.center,result.project.lane_gap)
                if wp.well.on_axis:
                    # Все четыре геометрические стороны осевой скважины:
                    # текущий алгоритм разрешает только выход к хребту.
                    hu,hv=(2.0,.8) if ori=='parallel' else (.8,2.0)
                    du,dv={'u+':(hu+.2,0),'u-':(-hu-.2,0),
                           'v+':(0,hv+.2),'v-':(0,-hv-.2)}[direc]
                    outlets[medium]=Pt(wp.point.u+du,wp.point.v+dv)
                if medium in outlets:
                    status='выбран' if wp.well.exit_direction==direc else None
                    if wp.well.on_axis:
                        status='геометрический порт; осевые отпайки пока выбираются по правилу прямого подвода к хребту'
                    if wp.well.exit_direction!='auto' and status is None:
                        status='выход закреплён пользователем; вариант не оценивался'
                    elif wp.well.rotation_locked and status is None and ori!=wp.orientation:
                        status='поворот скважины заблокирован'
                    add_start(outlets[medium],f'Скв. {owner} — {direc}',
                              wp.well.exit_direction==direc,status)
    elif 'врезке' in segment.purpose or 'пожарного узла' in segment.purpose:
        tie='врезке' in segment.purpose
        node=result.tie if tie else result.fire
        entry=(getattr(result,'decision_trace',{}) or {}).get(
              ('external','oil_product' if tie else 'fire'),{})
        if tie:
            add_start(node,'Точка врезки — единственная координата',True,
                      'точечный элемент: стороны выхода не рассматриваются')
        trials={q['direction']:q for q in entry.get('terminal_approaches',[])}
        for direc,du,dv in (() if tie else (('U+',0.6,0),('U-',-0.6,0),
                                        ('V+',0,0.6),('V-',0,-0.6))):
            test=trials.get(direc)
            if test:
                status=('оценён: '+str(test['cost']) if test.get('cost') is not None
                        else 'проверен: '+test.get('result','недопустим'))
            else:
                status='не проверен'
            add_start(Pt(node.u+du,node.v+dv),
                      ('Точка врезки' if tie else 'Пожарный узел')+' — '+direc,
                      status=status)
    else:
        add_start(segment.a,'Начало выделенного участка',True)
        add_start(segment.b,'Конец выделенного участка',True)

    trace=getattr(result,'decision_trace',{}) or {}
    records=(trace.get((owner,medium)) if owner is not None else
             trace.get(('external','oil_product')) if 'врезке' in segment.purpose else
             trace.get(('external','fire')) if 'пожарного узла' in segment.purpose else None)
    direct=trace.get('_direct_baseline',{}).get((owner,medium)) if owner is not None else None
    alternatives=list((records or {}).get('alternatives',[]))
    if direct is not records:
        alternatives += list((direct or {}).get('alternatives',[]))
    # Альтернативы по ВСЕМ четырём выходам (проверенные варианты из
    # реального предварительного поиска, а не реконструкция задним числом).
    origin_comparisons=[]
    if owner is not None:
        for entry in trace.get('_orientation_candidates',[]):
            item=entry.get('line_candidates',{}).get(f'{owner}:{medium}')
            if item is not None:
                origin_comparisons.append({
                    'exit':entry.get('exits',{}).get(owner,'auto'),
                    'pre_J':entry.get('pre_J'),
                    'final_J':entry.get('final_J'),
                    'status':entry.get('status'),
                    'choice':item.get('selected'),
                    'alternatives':list(item.get('alternatives',[])),
                    'rejected':dict(item.get('rejected',{})),
                })
    for record in origin_comparisons:
        alternatives.extend(record['alternatives'])
    checked={tuple(round(float(v),5) for v in item['target'])
             for item in alternatives if item.get('target') and len(item['target'])==2}
    points={}
    def add_target(q,label,checked_hint=False):
        key=_key(q)
        status='локально оценён' if checked_hint or key in checked else 'геометрический кандидат; оценка не подтверждена'
        old=points.get(key)
        if old is None or (old['status']!='локально оценён' and status=='локально оценён'):
            points[key]={'point':q,'label':label,'status':status}

    # Реальные точки, которые движок сохранил как рассмотренные.
    for item in alternatives:
        t=item.get('target')
        if t and len(t)==2:
            add_target(Pt(*map(float,t)),str(item.get('kind','Записанный кандидат')),True)
    plate=result.central_plate
    is_product_tie=('врезке' in segment.purpose.lower() and medium=='oil_product')
    if is_product_tie and plate is not None:
        from product_tie_rules import enumerate_tie_sites,central_side_sites
        occupied=[]
        axial_numbers={wp.well.number for wp in result.wells if wp.well.on_axis}
        for seg in result.segments:
            owner_number=_owner(seg.purpose)
            if owner_number not in axial_numbers or not seg.purpose.startswith('Отвод от скв.'):
                continue
            lane=result.lanes.get(seg.medium)
            if lane is None:continue
            for q in (seg.a,seg.b):
                if abs(q.v-lane)<1e-5:occupied.append(q.u)
        phase='active' if segment.phase=='active' else 'future'
        for site in central_side_sites(plate,result.lanes):
            add_target(site.point,'Центральная плита: боковой продуктовый порт (0,1 м)')
        for site in enumerate_tie_sites(result.segments,result.slabs,plate,occupied,
                                        required_phase=phase):
            kind=('Свободный дальний торец хребта' if site.kind=='end' else
                  'Опорная плита хребта: боковая врезка (0,1 м)')
            add_target(site.point,kind)
    if not is_product_tie and plate is not None and medium in SIDE_ONLY_PLATE_MEDIA and medium in result.lanes:
        a,b=plate.u_min+0.35,plate.u_max-0.35
        for u in sorted({a,b,(a+b)/2,*[a+i*.5 for i in range(int((b-a)/.5)+1)]}):
            if a-1e-6<=u<=b+1e-6:
                for v in (plate.v_min,plate.v_max):
                    add_target(Pt(u,v),'Боковой порт центральной плиты')
    if not is_product_tie:
        # Магистрали выбранной среды, включая продолжения после поворотов.
        trunk=[s for s in result.segments if s.medium==medium and s.purpose in (
            'Хребет','Перспективный хребет','Перспективное продление хребта')]
        origins=[p['point'] for p in source] or [segment.a]
        for s in trunk:
            if s.horizontal:
                l,r=sorted((s.a.u,s.b.u))
                samples={l,r,(l+r)/2}
                for start in origins:
                    proj=max(l,min(r,start.u))
                    samples.update((proj,max(l,min(r,proj-.5)),max(l,min(r,proj+.5))))
                for pt in (s.a,s.b):
                    samples.add(pt.u)
                for u in samples:
                    add_target(Pt(u,s.a.v),'Продуктовый/водяной тройник хребта')
            else:
                l,r=sorted((s.a.v,s.b.v))
                samples={l,r,(l+r)/2}
                for start in origins:
                    proj=max(l,min(r,start.v))
                    samples.update((proj,max(l,min(r,proj-.5)),max(l,min(r,proj+.5))))
                for v in samples:
                    add_target(Pt(s.a.u,v),'Тройник хребта')
        # Донорские нити, учитывая продолжения после отводов. Их концы и
        # перпендикулярные проекции образуют конечный набор точек на сегменте.
        for s in result.segments:
            if s.medium!=medium or s.purpose in ('Центральная плита: магистральная труба',) or s in trunk:
                continue
            if 'пожарного узла' in s.purpose:
                continue
            if owner is not None and f'скв. {owner}' in s.purpose:
                continue
            add_target(s.a,'Начало существующей нитки той же среды')
            add_target(s.b,'Конец существующей нитки той же среды')
            if s.horizontal:
                lo,hi=sorted((s.a.u,s.b.u))
                for start in origins:
                    u=max(lo,min(hi,start.u))
                    add_target(Pt(u,s.a.v),'Проекция на существующую нитку')
            else:
                lo,hi=sorted((s.a.v,s.b.v))
                for start in origins:
                    v=max(lo,min(hi,start.v))
                    add_target(Pt(s.a.u,v),'Проекция на существующую нитку')
    # Сортировка: сначала фактически оценённые, затем ближние к началу.
    origins=[entry['point'] for entry in source] or [segment.a]
    base=origins[0]
    ordered=sorted(points.values(),key=lambda entry:(
        0 if entry['status']=='локально оценён' else 1,
        abs(entry['point'].u-base.u)+abs(entry['point'].v-base.v),
        entry['point'].u,entry['point'].v))
    return {'starts':source,'ends':ordered,'owner':owner,'medium':medium,
            'actual_checked':len([x for x in ordered if x['status']=='локально оценён']),
            'per_start':origin_comparisons,
            'search_meta':trace.get('_start_search_meta',{}),
            'note':('Точка врезки имеет ровно один исходный порт; к хребту допускается подход только с внешней стороны НДС, плюс дальний свободный торец; боковые точки опор — через 0,1 м.' if is_product_tie else 'Порты и цели: геометрические кандидаты; полный непрерывный перебор не выполняется.')}


def format_points(summary,limit=None):
    def name(item):
        p=item['point']
        return f"U={p.u:.2f}; V={p.v:.2f} — {item['label']} [{item['status']}]"
    starts='\n'.join(f'{i}. {name(item)}' for i,item in enumerate(summary['starts'],1))
    ends='\n'.join(f'{i}. {name(item)}' for i,item in enumerate(
        summary['ends'][:limit] if limit else summary['ends'],1))
    lines=[f"\nВОЗМОЖНЫЕ НАЧАЛЬНЫЕ ПОРТЫ ({len(summary['starts'])}):",
           starts, f"\nВОЗМОЖНЫЕ КОНЕЧНЫЕ ТОЧКИ ({len(summary['ends'])}, реально локально оценённых {summary['actual_checked']}):",
           ends]
    meta=summary.get('search_meta') or {}
    if meta:
        lines.append('\nКОМБИНАЦИИ ВЫХОДОВ: всего '+str(meta.get('total_combinations','?'))
                     +'; предварительно рассмотрено '+str(meta.get('prechecked','?'))
                     +'; полная J рассчитана '+str(meta.get('full_scored','?'))
                     +'. Исчерпывающий полный перебор: '+('да' if meta.get('all_feasible_combinations_full_scored') else 'нет'))
    checks=summary.get('per_start',[])
    if checks:
        lines += ['\nПОИСК: КУДА ПРОБОВАЛИ ПОДКЛЮЧИТЬ КАЖДЫЙ ВЫХОД']
        for record in checks:
            state=(f"полная J={record['final_J']:.2f}" if record.get('final_J') is not None
                   else f"предв. J={record['pre_J']:.2f}" if record.get('pre_J') is not None
                   else 'без оценки J')
            lines.append(f"Выход {record['exit']}: {state} — {record['status']}; выбран локально: {record['choice']}; "
                         f"конечных кандидатов {len(record['alternatives'])}.")
            for item in record['alternatives']:
                coords=item.get('target')
                point=(f'({coords[0]:.2f};{coords[1]:.2f})' if coords else 'неизвестно')
                cost=item.get('score')
                price=f'{cost:.2f}' if cost is not None else '?'
                lines.append(f"  → {item.get('kind')} {point}: лок. {price}, "
                             f"длина {item.get('length','?')}, отводов {item.get('bends','?')}")
            if record.get('rejected'):
                lines.append('  Отказы: '+', '.join(f'{reason} ({n})' for reason,n in record['rejected'].items()))
    lines.extend(['',summary['note'],
                  'Перебор четырёх портов и точек подключения производится по конечным геометрическим кандидатам. Объяснение не означает, что проверялась каждая точка непрерывной линии.'])
    return '\n'.join(lines)
