"""v89: mandatory physical continuity audit for the actual chosen pipeline network.

A route that merely touches another off-axis well is NOT connected until their
combined same-medium pipe reaches the installed central plate.  The check is
run for every COMPLETE candidate, before pricing and before acceptance.

This is a topological audit of the model, not a hydraulic design check.
"""
from __future__ import annotations

from collections import defaultdict, deque
from routing import MEDIA_RU
from pipe_render import make_network

STRUCTURAL_ROOT = 'Центральная плита: магистральная труба'
EPS = 1e-5


def _pt(point):
    return (round(point.u, 6), round(point.v, 6))


def _required(survey, *, allow_missing_fire=False):
    """Mandatory core endpoints only: on-axis wells + fire.

    Off-axis wells and the product tie-in are intentionally excluded while
    external routing is being redesigned from scratch.
    """
    axial_ids = {wp.well.number for wp in survey.wells if wp.well.on_axis}
    for el in survey.elements:
        if el.status == 'inactive' or (allow_missing_fire and el.key == 'fire'):
            continue
        if el.kind == 'tie':
            continue
        if el.kind == 'well' and el.well_number not in axial_ids:
            continue
        for medium in el.media:
            yield el, medium


def _component(graph, roots):
    reached = set(roots)
    todo = deque(roots)
    while todo:
        for nxt in graph.get(todo.popleft(), ()):
            if nxt not in reached:
                reached.add(nxt)
                todo.append(nxt)
    return reached


def audit_mandatory_connections(survey, segments, selected, *, allow_missing_fire=False):
    """Return a factual audit or raise ValueError with exact affected elements.

    Only geometrically equal endpoints or an endpoint lying ON a continuous
    same-medium segment create a connection.  An unjoined perpendicular cross
    or a close parallel line is never a connection.  Future pipes cannot
    complete the path of an active well.  Each required well/tie/fire route
    must reach a physically modeled central-plate pipe of its own medium.
    """
    source_groups = defaultdict(list)
    for q in selected:
        source_groups[(q.source, q.medium)].append(q)
    requirements = list(_required(survey, allow_missing_fire=allow_missing_fire))
    problems = []
    for el, medium in requirements:
        paths = source_groups.get((el.key, medium), ())
        if len(paths) != 1:
            problems.append(f'{el.label} ({MEDIA_RU.get(medium, medium)}): '
                            f'ожидалось одно подключение, фактически {len(paths)}')
    expected = {(e.key, m) for e, m in requirements}
    for source, medium in source_groups:
        if (source, medium) not in expected:
            problems.append(f'{source} ({MEDIA_RU.get(medium, medium)}): '
                            'в итоговой сети есть неразрешённая или повторная связь')
    if problems:
        raise ValueError('НАРУШЕНА СВЯЗНОСТЬ СЕТИ: ' + '; '.join(problems[:5]))

    # The network builder splits a segment at another genuine segment ENDPOINT,
    # not at mere crossings without endpoints; this is exactly the tee geometry.
    graphs = {}
    for phase in ('active', 'future'):
        eligible = [s for s in segments if phase == 'future' or s.phase == 'active']
        graphs[phase] = {}
        for medium, edges in make_network(eligible).items():
            adj = defaultdict(set)
            for a, b, _edge_phase in edges:
                adj[a].add(b)
                adj[b].add(a)
            graphs[phase][medium] = dict(adj)

    reached = {}
    roots_count = {}
    for phase in ('active', 'future'):
        for medium in {m for _, m in requirements}:
            roots = set()
            for seg in segments:
                if (seg.medium == medium and seg.purpose == STRUCTURAL_ROOT
                        and (phase == 'future' or seg.phase == 'active')):
                    roots.add(_pt(seg.a))
                    roots.add(_pt(seg.b))
            graph = graphs[phase].get(medium, {})
            roots.intersection_update(graph)
            roots_count[(phase, medium)] = len(roots)
            reached[(phase, medium)] = _component(graph, roots)

    records = []
    for el, medium in requirements:
        q = source_groups[(el.key, medium)][0]
        phase = 'future' if el.status == 'future' else 'active'
        valid_points = reached[(phase, medium)]
        start = _pt(q.start)
        end = _pt(q.end)
        if len(q.path) < 2 or _pt(q.path[0]) != start or _pt(q.path[-1]) != end:
            problems.append(f'{el.label} ({MEDIA_RU.get(medium, medium)}): '
                            'начало или конец выбранного маршрута не совпадает с его портом')
        for a, b in zip(q.path, q.path[1:]):
            if _pt(a) == _pt(b) or (abs(a.u-b.u)>EPS and abs(a.v-b.v)>EPS):
                problems.append(f'{el.label} ({MEDIA_RU.get(medium, medium)}): '
                                'маршрут содержит нулевой или неортогональный отрезок')
                break
        linked = bool(roots_count[(phase, medium)] and start in valid_points and end in valid_points)
        if not linked:
            problems.append(f'{el.label} ({MEDIA_RU.get(medium, medium)}): '
                            f'нет непрерывного пути до центральной магистрали; '
                            f'конец U={q.end.u:.2f}; V={q.end.v:.2f}; '
                            f'назначение {q.target}')
        records.append({
            'source': q.source, 'label': el.label, 'medium': medium,
            'phase': phase, 'target': q.target,
            'start': [q.start.u, q.start.v], 'end': [q.end.u, q.end.v],
            'length': round(q.length, 4), 'elbows': q.elbows,
            'path': [[point.u, point.v] for point in q.path],
            'status': 'подключено к центральной магистрали' if linked else 'ОБРЫВ',
        })
    if problems:
        raise ValueError('НАРУШЕНА СВЯЗНОСТЬ СЕТИ: ' + '; '.join(problems[:5]))
    return {'status': ('промежуточная сеть без пожарного узла: все выбранные подключения достигли магистрали'
                       if allow_missing_fire else 'все обязательные подключения имеют путь до магистрали'),
            'checked': len(records), 'failed': 0, 'partial_nonfire': allow_missing_fire,
            'method': 'граф физических отрезков одной среды с настоящими Т-узлами',
            'records': records}


def format_actual_connection_report(result) -> str:
    """Detailed ONLY-as-run information, not hypothetical solver claims."""
    d = getattr(result, 'decision_trace', {}) or {}
    info = d.get('_integrity_audit')
    lines = ['ФАКТИЧЕСКИЙ РАСЧЁТ В ЭТОЙ ВЕРСИИ',
             'Маршруты — реальные координаты выбранных линий результата,',
             'а не примерная схема. Оптимум всего пространства НЕ доказан.', '']
    if not info:
        lines += ['Протокол обязательной проверки связности для этого результата НЕ СОХРАНЁН.',
                  'Результат следует пересчитать этой версией.']
        return '\n'.join(lines)
    lines += ['ПРОВЕРКА НЕПРЕРЫВНОСТИ',
              'Результат: '+info.get('status', 'неизвестно'),
              f"Проверено обязательных труб: {info.get('checked', 0)}; обрывов: {info.get('failed', '?')}.",
              'Контроль: от каждой необходимой коммуникации до центральной трубы',
              'ТОЙ ЖЕ среды; пересечение разных сред и близость без тройника не соединяют.', '']
    initial_candidates=d.get('_candidate_evaluations',{})
    if initial_candidates:
        lines += ['ВНУТРЕННИЙ ПОДБОР ДОПУСТИМОЙ ИСХОДНОЙ СЕТИ',
                  f"Сети без пожарной линии: проверено {initial_candidates.get('nonfire_checked', '?')}; "
                  f"с пожарной линией: {initial_candidates.get('fire_checked','?')}; "
                  f"принято полных допустимых конфигураций: {initial_candidates.get('complete_valid_networks','?')}."]
        for key in ('nonfire_rejection_reasons','fire_rejection_reasons'):
            reasons=initial_candidates.get(key,{})
            if reasons:
                lines.append('Реально зафиксированные причины отклонений ('+key+'):')
                for reason,count in sorted(reasons.items(),key=lambda p:-p[1])[:8]:
                    lines.append(f'  {count} × {reason}')
        lines.append('')
    search = d.get('_stepwise_search')
    if search:
        lines += ['ЭТАП «РАССЧИТАТЬ» — ФАКТИЧЕСКИЕ СЧЁТЧИКИ',
                  f"Шаг сетки: {search.get('grid_step_m', 'неизвестен')} м; "
                  f"точек подключения: {search.get('targets_checked', '?')}; "
                  f"попыток A*: {search.get('paths_examined', '?')}; "
                  f"узлов графа: {search.get('grid_nodes_expanded', '?')}.",
                  f"Ограничения поиска достигнуты: {search.get('grid_limit_hits', '?')}; "
                  f"восстановление исходной сети: {search.get('feasibility_recovery_used', False)}.", '']
    else:
        lines += ['Для этого результата счётчики предварительного поиска не сохранены.', '']
    joint=d.get('_joint_optimizer')
    if joint:
        lines += ['ПРОВЕРКИ ПАР И ТРОЕК В ПРЕДВАРИТЕЛЬНОМ ПОИСКЕ',
                  f"Полных испытаний: {joint.get('full_trials','?')}; "
                  f"отклонено: {joint.get('rejected','?')}; "
                  f"пар в отборе: {joint.get('pairs_examined','?')}; "
                  f"троек: {joint.get('triples_examined','?')}."]
        reasons=joint.get('rejection_reasons',{})
        for reason,count in sorted(reasons.items(),key=lambda pair:-pair[1])[:8]:
            lines.append(f'  {count} × {reason}')
        lines.append('')
    optimization=d.get('_stepwise_optimization')
    if optimization:
        lines += ['ЭТАП «ОПТИМИЗИРОВАТЬ» — ФАКТИЧЕСКИЕ СЧЁТЧИКИ',
                  f"Полных испытаний: {optimization.get('attempted','?')}; "
                  f"отклонено: {optimization.get('rejected','?')}; "
                  f"принято улучшений: {optimization.get('accepted','?')}.",
                  f"Проверенных предложений пучков: {optimization.get('corridors_tested','?')}; "
                  f"из них трёх сред: {optimization.get('triple_corridors_tested','?')}.",
                  f"Исходная смета: {optimization.get('independent_cost', 0):,.2f} ₽; "
                  f"выбранная: {optimization.get('best_cost',0):,.2f} ₽.",
                  'Ни один вариант с обрывом обязательной коммуникации не допускается',
                  'к сравнению стоимости. Отклонения по связности регистрируются отдельно.']
        reason_counts=optimization.get('rejection_reasons',{})
        if reason_counts:
            lines.append('Подтверждённые причины отклонений:')
            for reason,count in sorted(reason_counts.items(), key=lambda x:-x[1]):
                lines.append(f'  {count} × {reason}')
        samples=optimization.get('rejection_examples', [])
        if samples:
            lines.append('Фрагменты фактически отклонённых испытаний (не полный журнал):')
            for example in samples:
                lines.append('  '+str(example))
        lines.append('')
    else:
        lines += ['Этап «Оптимизировать» ещё не выполнялся или его счётчики не сохранены.', '']
    lines.append('ВСЕ ФАКТИЧЕСКИ ВЫБРАННЫЕ КОММУНИКАЦИИ')
    for entry in info.get('records', []):
        lines.append(f"\n{entry['label']} | {MEDIA_RU.get(entry['medium'],entry['medium'])} | {entry['status']}")
        lines.append(f"  Конец: {entry['target']}; стадия: {entry['phase']}; "
                     f"труба {entry['length']:.3f} м; отводов в исходном маршруте {entry['elbows']}.")
        for j,point in enumerate(entry['path']):
            lines.append(f'  {j+1:02d}: U={point[0]:.3f}; V={point[1]:.3f}')
    lines += ['', 'ЧТО РЕАЛЬНО ДЕЛАЕТ ДВИЖОК',
              '1. Рассчитать: генерирует порты центральной плиты и хребта;',
              '   A* по конечной ортогональной сетке с направлением предыдущего ребра;',
              '   строит независимую сеть, исключая запрещённые соединения.',
              '2. Для каждой проверенной сети: строит физические рёбра по средам;',
              '   от каждого требуемого источника ищет путь до трубы центральной плиты;',
              '   если хотя бы одна ветвь изолирована — отклоняет ВСЮ сеть.',
              '3. Только после этого: размещает обязательные плиты, заполнители',
              '   и считает полную стоимость по действующим расценкам.',
              '4. Оптимизировать: сравнивает несколько изменений маршрутов,',
              '   тройники своей среды и параллельные пучки разных сред.',
              '   Каждый вариант заново проходит пункты 2–3.',
              '5. Количество исследований ограничено настройками и временем;',
              '   доказательство глобального минимума отсутствует.',
              'Отображаются только реальные данные данного расчёта.']
    return '\n'.join(lines)
