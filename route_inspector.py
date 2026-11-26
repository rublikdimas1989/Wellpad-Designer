"""Проверяемая диагностика выбранного трубного участка.

Диагностика не подменяет протокол вычислений предположениями. Записываем
только варианты, которые действительно были оценены при данном построении.
"""
from __future__ import annotations

import math
import re


def _loc(point):
    return f'U={point.u:.2f}, V={point.v:.2f}'


def _cost(value):
    return 'не сохранялась' if value is None else f'{value:.2f} усл. ед.'


def _owner_from_purpose(purpose):
    found=re.search(r'скв\.\s*(\d+)',purpose,re.IGNORECASE)
    return int(found.group(1)) if found else None


def _connected_run(result, index):
    """Вся физически связанная труба данной среды, ВКЛЮЧАЯ отводы.

    Отметка назначения и разделение на прямые сегменты не разрывают связь.
    Для действующей линии перспективные неподключённые ветки не добавляем.
    """
    from continuous_routes import ContinuousNetwork
    start=result.segments[index]
    network=ContinuousNetwork(result.segments,start.medium,
                              active_only=(start.phase=='active'))
    component=network.component(start.a)
    return [s for s in result.segments if s.medium==start.medium
            and (start.phase!='active' or s.phase=='active')
            and (round(s.a.u,6),round(s.a.v,6)) in component
            and (round(s.b.u,6),round(s.b.v,6)) in component]


def _full_connection_path(result, index, owner):
    """Путь от устья или выделенного сегмента до главной нитки среды.

    Расстояние считается по целому графу, а не по именам сегментов.
    Повороты входят в стоимость каждого маршрута ровно один раз.
    """
    from continuous_routes import ContinuousNetwork
    from routing import well_medium_outlets
    selected=result.segments[index]
    targets=[]
    for s in result.segments:
        if s.medium!=selected.medium:continue
        if s.purpose in ('Хребет','Перспективный хребет',
                         'Перспективное продление хребта',
                         'Центральная плита: магистральная труба'):
            targets.extend((s.a,s.b))
    if not targets:
        return None
    origins=[]
    if owner is not None:
        wp=next((w for w in result.wells if w.well.number==owner),None)
        if wp:
            actual=well_medium_outlets(wp,result.trunk_side,result.center,
                                       result.project.lane_gap)
            if selected.medium in actual:
                origins.append(actual[selected.medium])
    origins.extend((selected.a,selected.b))
    for origin in origins:
        network=ContinuousNetwork(result.segments,selected.medium,
             active_only=(selected.phase=='active'),extra_points=[origin])
        path=network.shortest_path(origin,targets,
              elbow_cost=result.project.elbow_cost,
              pipe_cost=result.project.pipe_cost_per_m)
        if path is not None:
            return path
    return None


def _describe_choice(result, key):
    trace=getattr(result,'decision_trace',{}) or {}
    entry=trace.get(key)
    original=False
    if entry is None or (not entry.get('alternatives') and isinstance(key[0],int)):
        original_entry=trace.get('_direct_baseline',{}).get(key)
        if original_entry:
            entry=entry or original_entry
            original=True
    if entry is None:
        return ['Журнал кандидатов для этой линии не сохранён.',
                'Это не доказывает, что другие способы подключения отсутствуют.']
    lines=['ИСТОРИЯ ВЫБОРА',
           f"Вариант на данном этапе: {entry.get('choice','—')}.",
           f"Оценка при выборе: {_cost(entry.get('choice_cost'))}."]
    if entry.get('orientation'):
        lines.append('Ориентация скважины: '+ ('вдоль НДС' if entry['orientation']=='parallel' else 'поперёк НДС'))
    if entry.get('note'):
        lines.append(entry['note'])
    if original:
        lines.append('Ниже дополнительно приведены оценки исходной РАЗДЕЛЬНОЙ прокладки. Они не являются ценами кандидатов текущего пучка.')
    candidates=entry.get('alternatives') or []
    if not candidates and isinstance(key[0],int):
        candidates=trace.get('_direct_baseline',{}).get(key,{}).get('alternatives',[])
        if candidates:
            lines.append('Исходные альтернативы до оптимизации пучка (локальные оценки, НЕ итоговая J):')
    if candidates:
        lines.append('\nДОПУСТИМЫЕ КАНДИДАТЫ, ФАКТИЧЕСКИ ОЦЕНЁННЫЕ В ПОИСКЕ:')
        for n,item in enumerate(candidates[:12],1):
            point=item.get('target')
            target=(f"U={point[0]:.2f}, V={point[1]:.2f}" if point else '—')
            lines.append(f"{n}. {item.get('kind','Маршрут')} → {target}; "
                         f"локальная оценка {_cost(item.get('score'))}; "
                         f"длина {item.get('length',0):.2f} м; отводов {item.get('bends','?')}.")
        if len(candidates)>12:
            lines.append(f'...и ещё {len(candidates)-12} показанных в журнале альтернатив.')
        lines.append('Почему отклонены допустимые варианты: на соответствующем локальном этапе проиграли по цене, либо по вторичному критерию. Последующие глобальные перестроения могли изменить выбор.')
    else:
        lines.append('На этом этапе отдельные альтернативы не сохранялись. Не следует считать их отклонёнными по стоимости.')
    terminal=entry.get('terminal_approaches',[])
    if terminal:
        lines.append('\nЧЕТЫРЕ ПОДХОДА К ФУНКЦИОНАЛЬНОЙ ТОЧКЕ:')
        for choice in terminal:
            cost=choice.get('cost')
            lines.append(f"• {choice['direction']}: {choice.get('result','?')}"
                         + (f'; стоимость локальной сети {cost:.2f}' if cost is not None else ''))
    rejected=entry.get('rejected',{})
    if rejected:
        lines.append('\nНЕПРОШЕДШИЕ ПРОВЕРКИ (сводно, без выдуманной точной причины для каждой точки):')
        for reason,count in rejected.items():
            if count: lines.append(f'• {reason}: {count} случаев')
    return lines


def inspect_segment(result, segment_index):
    if segment_index < 0 or segment_index >= len(result.segments):
        raise IndexError('Выбранного участка нет в текущем расчёте')
    seg=result.segments[segment_index]
    run=_connected_run(result,segment_index)
    from routing import MEDIA_RU, STATUS_RU
    owner=_owner_from_purpose(seg.purpose)
    if 'пожарного узла' in seg.purpose.casefold():
        key=('external','fire')
    elif 'от центра к врезке' in seg.purpose.casefold():
        key=('external','oil_product')
    elif owner is not None:
        key=(owner,seg.medium)
    else:
        key=None
    lines=[f'УЧАСТОК №{segment_index+1}',
           f'Среда: {MEDIA_RU.get(seg.medium,seg.medium)}',
           f'Состояние: {STATUS_RU.get(seg.phase,seg.phase)}',
           f'Назначение в модели: {seg.purpose}',
           f'Начало: {_loc(seg.a)}', f'Конец: {_loc(seg.b)}',
           f'Длина выбранного участка: {seg.length:.3f} м',
           f'Связанные участки этой среды (включая повороты и ответвления): {sum(s.length for s in run):.3f} м ({len(run)} сегментов).',
           f'Условная стоимость длины этого участка: {seg.length*result.project.pipe_cost_per_m:.2f} ед.']
    if owner is not None:lines.append(f'Связанная скважина по назначению участка: №{owner}.')
    path=_full_connection_path(result,segment_index,owner)
    if path is not None:
        lines.extend([
            '', 'ЕДИНЫЙ ПУТЬ А → Б ЧЕРЕЗ ВСЕ ОТВОДЫ',
            f'Длина непрерывного пути до хребта/центральной плиты: {path.length:.3f} м.',
            f'Поворотов на всём пути: {path.elbows}.',
            f'Стоимость трубы + поворотов на этом пути: '
            f'{path.length*result.project.pipe_cost_per_m+path.elbows*result.project.elbow_cost:.2f} усл. ед.',
            'Переход через отвод и смена названия сегмента НЕ разрывают путь. '
            'Тройники, пересечения и экономия общих коридоров учитываются отдельно в общей J.'
        ])
    else:
        lines.append('Единый путь до главной нитки среды в текущем графе не найден (возможен обрыв).')
    lines+=['', 'ВАЖНО О СТОИМОСТИ',
            'Это стоимость только трубы данного участка. Отводы, тройники, пересечения, пучки и другие части сети учитываются в общем J, не распределённом по одиночным сегментам.',
            f"Текущая общая стоимость сети J: {_cost(result.metrics.get('optimized_score'))}.", '']
    if key:
        lines+=_describe_choice(result,key)
    elif seg.purpose in ('Хребет','Перспективный хребет','Перспективное продление хребта'):
        lines+=['ПРИЧИНА РАСПОЛОЖЕНИЯ',
                'Хребтовая магистраль строится по заданной оси на фиксированной поперечной позиции соответствующей среды.',
                'Она продолжается от центральной плиты до фактических точек подключения; набор альтернативных трасс для каждого метра хребта не перебирается.']
    elif 'Центральная плита' in seg.purpose:
        lines+=['ПРИЧИНА РАСПОЛОЖЕНИЯ','Магистраль находится внутри центральной плиты; геометрия зафиксирована проектным правилом.']
    else:
        lines+=['ПРИЧИНА РАСПОЛОЖЕНИЯ',
                'Этот участок сформирован правилами геометрии или поздней оптимизацией. Индивидуальные отвергнутые варианты для него не записаны.']
    trace=getattr(result,'decision_trace',{}) or {}
    # v73: report scores of COMPLETE networks for different oil-product
    # attachment families, not misleading prices of isolated route segments.
    if key==('external','oil_product') and trace.get('_product_network_families'):
        lines+=['','ПОДКЛЮЧЕНИЕ ВРЕЗКИ: СРАВНЕНИЕ ГОТОВЫХ СЕТЕЙ']
        family_names={'initial_selected':'Первоначально выбранная сеть',
                      'side_safe':'Боковой порт центральной плиты',
                      'trunk_nearest':'Подключение к продуктовому хребту'}
        for item in trace['_product_network_families']:
            label=family_names.get(item.get('mode'),str(item.get('mode')))
            if item.get('full_J') is None:
                lines.append('• '+label+': отклонено — '+item.get('reason','нет допустимой сети'))
            else:
                line='• '+label+f": J={item['full_J']:.2f}"
                if item.get('actual_choice'):
                    line+=f", фактическое подключение: {item['actual_choice']}"
                if item.get('port'):
                    line+=f", точка U={item['port'][0]:.2f}, V={item['port'][1]:.2f}"
                if item.get('shared_groups'):
                    line+=f", группы: {item['shared_groups']}"
                lines.append(line)
        lines.append('Выбрана только допустимая сеть с лучшей общей стоимостью; '
                     'этот конечный перебор не является доказательством глобального минимума.')
    if key==('external','oil_product') and trace.get('_bundle_search'):
        bundle=trace['_bundle_search']
        lines+=['','ПОИСК ОБЩЕГО КОРИДОРА ВОКРУГ ПРОДУКТОВОЙ ТРУБЫ',
                f"Ближайшие внеосевые скважины: {bundle.get('nearby_wells',[])}.",
                f"Проверенные группы: {bundle.get('attempted_groups',[])}.",
                f"Полных перестроений: {bundle.get('evaluated',0)}, геометрически допустимых: {bundle.get('geometrically_valid',0)}."]
        for reason,count in list(bundle.get('rejections',{}).items())[:5]:
            lines.append(f'• {reason}: {count}')
        lines.append(bundle.get('note',''))
    comparisons=trace.get('_global_comparison')
    if comparisons and owner is not None:
        lines+=['','СРАВНЕНИЕ ЦЕЛЫХ СЕТЕЙ (ДЕЙСТВИТЕЛЬНО ВЫПОЛНЕННОЕ)',
                f"Прямая/раздельная базовая сеть: J={comparisons['direct_J']:.2f}; отводов {comparisons['direct_elbows']}; труб {comparisons['direct_pipe_m']:.2f} м.",
                f"Оптимизированная сеть: J={comparisons['optimized_J']:.2f}; отводов {comparisons['optimized_elbows']}; труб {comparisons['optimized_pipe_m']:.2f} м.",
                f"Группы общих коридоров: {comparisons.get('groups',[])}.",
                comparisons.get('note','')]
    options=trace.get('_orientation_candidates',[])
    if owner is not None and options:
        lines+=['','РЕАЛЬНО ПРОВЕРЕННЫЕ ОРИЕНТАЦИИ СКВАЖИН (ОЦЕНКИ ЦЕЛЫХ СЕТЕЙ)']
        for item in options[:20]:
            orient=item.get('orientations',{})
            label=', '.join(f'№{i}: '+('вдоль' if o=='parallel' else 'поперёк')
                            for i,o in sorted(orient.items()) if any(w.well.number==i and not w.well.on_axis for w in result.wells))
            final=(f"J={item['final_J']:.2f}" if item.get('final_J') is not None
                   else f"предв. J={item['pre_J']:.2f} (пучки НЕ пересчитаны)")
            lines.append(f"• {label}; {final}; отводов {item['elbows']}; {item['status']}.")
        if len(options)>20:lines.append('...другие оценённые варианты не показаны в краткой карточке.')
    lines+=['','ОГРАНИЧЕНИЯ ДИАГНОСТИКИ',
            'Журнал содержит реально рассмотренные варианты отдельных этапов расчёта, но не является полным деревом поиска и не доказывает глобальный оптимум.',
            'Промежуточная локальная оценка и итоговая J могут отличаться. Варианты, не попавшие в перебор, нельзя считать отклонёнными.']
    return '\n'.join(lines)
