"""Осевое ядро куста — единственный активный расчёт на текущем этапе.

Строит:
  • размещение элементов (скважины, пожарный узел; врезка — только координата);
  • хребет по осевым скважинам;
  • осевые отпайки к хребту;
  • центральную плиту;
  • пожарный узел и его плиты;
  • плиты на оси;
  • сметные показатели стоимости.

Внешние подключения (внеосевые скважины и точка врезки) НАМЕРЕННО
не прокладываются — старый слой условий удалён, алгоритм будет
переработан отдельно.
"""
from __future__ import annotations

from copy import deepcopy

from catalog_solver import (_available, _link_cost, _make_result, link_signature)
from network_cost import cost_components, estimate_score
from planning_workflow import generate_links, make_survey


def is_core_source(el, survey) -> bool:
    """Только осевые скважины и пожарный узел участвуют в сети."""
    if el.status == 'inactive':
        return False
    if el.kind == 'fire':
        return True
    if el.kind == 'tie':
        return False
    if el.kind != 'well' or el.well_number is None:
        return False
    wp = next((w for w in survey.wells if w.well.number == el.well_number), None)
    return wp is not None and wp.well.on_axis


def core_catalog(survey) -> dict:
    """Каталог связей только для осевых скважин и пожарного узла."""
    out = {}
    for el in survey.elements:
        if not is_core_source(el, survey):
            continue
        out[el.key] = generate_links(survey, el.key)
    return out


def _plate_allowed(project, survey, name: str) -> bool:
    if project.center_position == 'left_auto' and name != 'left':
        return False
    if project.center_position not in ('auto', 'left_auto') and not project.center_position.startswith(name):
        return False
    if project.center_x is not None:
        last = max((w.point.u for w in survey.wells if w.well.on_axis), default=0.)
        right = survey.frame.uv(project.center_x, project.center_y).u > last
        if (name == 'right') != right:
            return False
    return True


def _pick_cheapest(links, project, name: str):
    pool = [q for q in links
            if q.path and q.status != 'недопустим' and _available(q, name)]
    if not pool:
        return None
    return min(pool, key=lambda q: (_link_cost(q, project), q.elbows, q.length, q.target))


def build_axis_core(project, survey=None, *, progress=None):
    """Собрать осевую сеть + пожарный узел. Без внешних трасс."""
    working = deepcopy(project)
    survey = survey or make_survey(working)
    if progress:
        progress('stage', message='Осевое ядро: хребет, отпайки, центральная плита…')
    catalog = core_catalog(survey)

    axial_keys = [el.key for el in survey.elements
                  if is_core_source(el, survey) and el.kind == 'well']
    fire_el = next((el for el in survey.elements if el.kind == 'fire'), None)

    nonfire_by_side = {}
    failures = []
    for name, _plate in survey.plate_variants:
        if not _plate_allowed(working, survey, name):
            continue
        selected = []
        ok = True
        for key in axial_keys:
            el = next(e for e in survey.elements if e.key == key)
            for medium in el.media:
                if medium == 'oil_measure':
                    continue
                pick = _pick_cheapest(
                    [q for q in catalog.get(key, ()) if q.medium == medium],
                    working, name)
                if pick is None:
                    failures.append(f'{el.label}/{medium}: нет осевой отпайки для плиты {name}')
                    ok = False
                    break
                selected.append(pick)
            if not ok:
                break
        if not ok:
            continue
        try:
            result = _make_result(survey, name, selected)
        except (ValueError, AssertionError) as exc:
            failures.append(f'{name}: {exc}')
            continue
        nonfire_by_side[name] = (result, selected)

    if not nonfire_by_side:
        detail = '; '.join(failures[:4]) if failures else 'нет допустимых осевых вариантов'
        raise ValueError('Осевое ядро: не удалось собрать хребет и осевые отпайки. ' + detail)

    chosen_side = min(
        nonfire_by_side,
        key=lambda name: (
            round(estimate_score(nonfire_by_side[name][0]), 5),
            nonfire_by_side[name][0].metrics.get('topological_elbows', 0),
            0 if name == 'left' else 1,
        ),
    )
    base_result, axial_selected = nonfire_by_side[chosen_side]

    if progress:
        progress('stage', message=f'Центральная плита: {chosen_side}; подключение пожарного узла…')

    chosen = base_result
    fire_length = 0.0
    if fire_el is not None and fire_el.status != 'inactive':
        fire_links = sorted(
            [q for q in catalog.get('fire', ())
             if q.path and q.status != 'недопустим' and _available(q, chosen_side)],
            key=lambda q: (_link_cost(q, working), q.elbows, q.length, q.target),
        )
        if not fire_links:
            raise ValueError(
                f'Осевое ядро: плита {chosen_side} зафиксирована, но нет допустимого '
                'пожарного подключения к её торцу или торцу хребта.')
        fire_failures = []
        attached = None
        for fl in fire_links:
            try:
                trial = _make_result(survey, chosen_side, (*axial_selected, fl))
            except (ValueError, AssertionError) as exc:
                fire_failures.append(str(exc).split(';')[0][:160])
                continue
            attached = (trial, fl)
            break
        if attached is None:
            raise ValueError(
                'Осевое ядро: пожарный узел не совместим с осевой сетью. '
                + (fire_failures[0] if fire_failures else ''))
        chosen, fl = attached
        fire_length = fl.length
        axial_selected = (*axial_selected, fl)

    costs = cost_components(chosen)
    chosen.metrics['optimized_score'] = float(estimate_score(chosen))
    chosen.metrics['cost_pipe'] = float(costs['pipe'])
    chosen.metrics['cost_elbows'] = float(costs['elbows'])
    chosen.metrics['cost_tees'] = float(costs['tees'])
    chosen.metrics['cost_small_plates'] = float(costs['small_plates'])
    chosen.metrics['core_mode'] = 1.0
    chosen.metrics['fire_connection_length_m'] = float(fire_length)
    chosen.decision_trace['_axis_core'] = {
        'mode': 'axis_core_only',
        'central_plate': chosen_side,
        'axial_links': len([q for q in axial_selected if q.source.startswith('well:')]),
        'fire_connected': any(q.source == 'fire' for q in axial_selected),
        'fire_length_m': round(fire_length, 3),
        'external_routing': 'disabled',
        'score': round(estimate_score(chosen), 2),
        'costs': {k: round(v, 2) for k, v in costs.items()},
        'catalog_hashes': [link_signature(q) for q in axial_selected],
    }
    # Keep hashes compatible with older UI/inspector paths.
    chosen.decision_trace['_catalog_hashes'] = [
        link_signature(q) for q in axial_selected]
    chosen.decision_trace['_selected_links'] = [
        {
            'source': q.source, 'medium': q.medium, 'target': q.target,
            'length': round(q.length, 4), 'elbows': int(q.elbows),
            'independent_cost': round(_link_cost(q, working), 2),
        }
        for q in axial_selected
    ]
    offaxis_n = sum(1 for w in survey.wells if not w.well.on_axis and w.well.status != 'inactive')
    chosen.notes.clear()
    chosen.notes.append(
        f'Осевое ядро: центральная плита «{chosen_side}», '
        f'осевых отпаек {sum(1 for q in axial_selected if q.source.startswith("well:"))}, '
        f'пожарный узел {"подключён" if fire_length else "не требуется"}.')
    chosen.notes.append(
        f'Смета ядра: трубы {costs["pipe"]:,.0f} ₽ · отводы {costs["elbows"]:,.0f} ₽ · '
        f'тройники {costs["tees"]:,.0f} ₽ · малые плиты {costs["small_plates"]:,.0f} ₽ · '
        f'итого {estimate_score(chosen):,.0f} ₽.')
    if offaxis_n:
        chosen.notes.append(
            f'Осевое ядро готово; далее независимый поиск внешних трасс '
            f'({offaxis_n} внеосевых скважин + врезка).')
    if progress:
        progress('best_network', message='Осевое ядро собрано',
                 cost=round(estimate_score(chosen), 0), segments=chosen.segments)
    return chosen, catalog, {
        'mode': 'axis_core_only',
        'central_plate': chosen_side,
        'paths_per_slot': 0,
        'pool_sizes': {k: len(v) for k, v in catalog.items()},
        'external_routing': 'disabled',
    }
