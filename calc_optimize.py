"""Алгоритмы кнопок «Рассчитать» и «Оптимизировать».

Этот файл — точка входа для UI. Внутри вызываются осевое ядро и
внешние трассы; сюда же сводится шкала прогресса 0…100 %.
"""
from __future__ import annotations

from copy import deepcopy


def _emit(progress, percent: float, message: str, **info):
    if not progress:
        return
    progress('stage', message=message, percent=max(0.0, min(100.0, float(percent))), **info)


def run_calculate(project, survey=None, *, progress=None, app_root=None, name_hint=''):
    """Кнопка «Рассчитать»: ядро + независимые внешние + снимок JSON.

    Progress events включают поле percent (0…100) для шкалы в UI.
    """
    from calculation_snapshot import save_calculation_snapshot
    from axis_core import build_axis_core
    from external_routes import PATHS_KEPT, attach_external_independent, external_slots
    from catalog_solver import _link_cost, link_signature
    from network_integrity import audit_mandatory_connections
    from planning_workflow import make_survey
    from network_cost import estimate_score

    working = deepcopy(project)
    _emit(progress, 2, 'Подготовка осевого ядра…')

    def core_progress(event, **info):
        # Ядро — примерно 0…20 %
        msg = info.pop('message', None) or event
        _emit(progress, 8 if event == 'stage' else 18, msg, **info)

    initial, catalog, search = build_axis_core(working, survey, progress=core_progress)
    survey = make_survey(working)
    _emit(progress, 22, 'Проверка связности осевого ядра…')
    try:
        from incremental_network import _selected_links
        initial.decision_trace['_integrity_audit'] = audit_mandatory_connections(
            survey, initial.segments, _selected_links(initial, catalog),
            allow_missing_fire=False)
    except ValueError as exc:
        initial.notes.append(f'Проверка связности ядра: {exc}')

    slots = external_slots(survey)
    n_slots = max(1, len(slots))

    def ext_progress(event, **info):
        # Внешние слоты — 25…90 %
        slot_i = info.get('slot_index')
        slot_n = info.get('slot_total') or n_slots
        if slot_i is not None and slot_n:
            base = 25.0 + 65.0 * (float(slot_i) / float(slot_n))
            frac = float(info.get('slot_frac', 0.0))
            pct = base + 65.0 * frac / float(slot_n)
        else:
            pct = float(info.pop('percent', 50) if 'percent' in info else 50)
        msg = info.pop('message', None) or event
        if event == 'source_done':
            msg = f"Готово: {info.get('source','')} · вариантов {info.get('candidate',0)}"
        elif event == 'route_candidate':
            msg = (f"Поиск {info.get('source','')} · {info.get('medium','')} · "
                   f"путей {info.get('candidate',0)}")
        _emit(progress, pct, msg, **info)

    _emit(progress, 25, f'Внешние подключения: слотов {len(slots)}…')
    initial, ext_catalog, pool, ext_stats = attach_external_independent(
        initial, working, survey, progress=ext_progress)
    for key, links in ext_catalog.items():
        catalog.setdefault(key, []).extend(links)

    initial.decision_trace['_stepwise_search'] = {
        **search, **ext_stats,
        'mode': 'axis_core_plus_independent_external',
        'paths_per_slot': PATHS_KEPT,
        'global_optimality_proven': False,
    }
    initial.decision_trace['_external_pool'] = {
        k: [{'signature': link_signature(q), 'length': q.length, 'elbows': q.elbows,
             'target': q.target, 'cost': round(_link_cost(q, working), 2)}
            for q in variants]
        for k, variants in pool.items()
    }

    _emit(progress, 92, 'Сохранение снимка расчёта…')
    snap = save_calculation_snapshot(
        working, catalog, initial,
        search=initial.decision_trace['_stepwise_search'],
        app_root=app_root, name_hint=name_hint or 'calc')
    initial.decision_trace['_calculation_snapshot'] = str(snap)
    initial.notes.append(f'Снимок расчёта сохранён: {snap.name} (папка «Снимки_расчёта»).')

    _emit(progress, 100, 'Расчёт завершён',
          cost=round(float(initial.metrics.get('optimized_score', 0)), 0),
          segments=initial.segments)
    return initial, catalog, initial.decision_trace['_stepwise_search']


def run_optimize(project, survey, catalog, initial, *, progress=None, limit=120,
                 app_root=None, name_hint=''):
    """Кнопка «Оптимизировать»: алгоритм в optimize_algorithm.py + JSON-снимок."""
    from optimize_algorithm import rebuild_pool_from_catalog, run_optimization_search
    from optimization_snapshot import save_optimization_snapshot
    from planning_workflow import make_survey

    working = deepcopy(project)
    work_survey = survey or make_survey(working)
    _emit(progress, 5, 'Сборка пула внешних путей…')
    pool = rebuild_pool_from_catalog(catalog, initial, working)

    if not pool:
        initial.notes.append(
            'Оптимизировать: нет запомненного пула внешних путей — сначала «Рассчитать».')
        initial.decision_trace['_stepwise_optimization'] = {
            'skipped': True, 'reason': 'empty_pool'}
        _emit(progress, 100, 'Оптимизация пропущена: пустой пул')
        return initial

    def opt_progress(event, **info):
        if event == 'optimization':
            cand = float(info.get('candidate', 0))
            total = max(1.0, float(info.get('total', limit)))
            pct = 10.0 + 85.0 * min(1.0, cand / total)
            msg = (f"Оптимизация {int(cand)}/{int(total)} · "
                   f"лучшая: {info.get('best', 0):,.0f} ₽")
            info.pop('message', None)
        else:
            pct = float(info.pop('percent', 50))
            msg = info.pop('message', None) or event
        _emit(progress, pct, msg, **info)

    _emit(progress, 10, f'Перебор комбинаций · слотов {len(pool)}…')
    result, report = run_optimization_search(
        working, work_survey, initial, pool,
        progress=opt_progress, limit=max(limit, 80))

    _emit(progress, 92, 'Сохранение снимка оптимизации…')
    snap = save_optimization_snapshot(
        working, pool, result, report,
        app_root=app_root, name_hint=name_hint or 'opt')
    result.decision_trace['_optimization_snapshot'] = str(snap)
    result.notes.append(f'Снимок оптимизации сохранён: {snap.name} (папка «Снимки_оптимизации»).')

    _emit(progress, 100, 'Оптимизация завершена',
          cost=round(float(result.metrics.get('optimized_score', 0)), 0),
          segments=result.segments)
    return result
