"""Внешние подключения: внеосевые скважины и точка врезки (v92).

«Рассчитать» — независимое самое дешёвое подключение каждого элемента
к допустимым точкам (бок хребта на хребтовых плитах / дальний торец /
бок центральной плиты со стороны элемента). Варианты запоминаются.
Линии и плиты разных элементов друг друга не видят.

«Оптимизировать» — перебор запомненных вариантов: общая труба одной
среды оплачивается один раз; совпадающие плиты 1,2×3 — один раз.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from itertools import product
from math import inf

from routing import (
    OFFAXIS_MIN_STRAIGHT, Pt, Segment, Slab,
    inner_trunk_parallel, line_phase, no_immediate_uturn,
    protected_line_ok, trunk_inner_zone,
    well_connection_normal, well_medium_outlets,
)
from planning_workflow import (
    DIRS, Element, LinkCandidate, Survey, _compact,
    make_survey, shortest_paths,
)
from catalog_solver import _link_cost, link_signature, path_signature
from network_cost import cost_components, estimate_score, count_small_plates
from product_tie_rules import (
    BRANCH_CLEARANCE, END_MARGIN, STEP, TieSite, candidate_gates,
    central_side_sites, end_outward_sign, enumerate_tie_sites,
    object_on_trunk_external_side, preview_support_slabs, tick_values,
)
from topology_rules import external_trunk_approach, external_trunk_gate

# Сколько вариантов помнить на (элемент, среда) после «Рассчитать»
PATHS_KEPT = 40
# Отсечка «нерациональных» путей относительно лучшего к той же точке
IRRATIONAL_LEN = 3.0          # длина > manhattan * коэффициент
IRRATIONAL_COST = 2.5         # стоимость > лучшей к точке * коэффициент
# Плиты внешних линий (временное правило, единое для всех)
EXT_PLATE_LEN = 3.0
EXT_PLATE_WID = 1.2
EXT_PLATE_GAP = 3.0           # целевой зазор между плитами (мин. 3, макс. 6)
EXT_PLATE_GAP_MAX = 6.0
EXT_LEAD = 1.0                # отступ первой плиты от устья / точки


def external_slots(survey: Survey):
    """Слоты внешних подключений: (element, medium)."""
    slots = []
    for el in survey.elements:
        if el.status == 'inactive':
            continue
        if el.kind == 'tie':
            if 'oil_product' not in el.media:
                continue
            if not any(s.medium == 'oil_product' for s in survey.trunk_segments):
                continue
            slots.append((el, 'oil_product'))
            continue
        if el.kind != 'well' or el.well_number is None:
            continue
        wp = next((w for w in survey.wells if w.well.number == el.well_number), None)
        if wp is None or wp.well.on_axis:
            continue
        for medium in el.media:
            if medium == 'oil_measure':
                continue
            slots.append((el, medium))
    return slots


def _occupied_u(survey: Survey, medium: str) -> tuple[float, ...]:
    vals = []
    for wp in survey.wells:
        if not wp.well.on_axis or wp.well.status == 'inactive':
            continue
        if medium not in dict(wp.connections):
            continue
        vals.append(wp.point.u)
    return tuple(vals)


def connection_sites(survey: Survey, plate, medium: str, source_v: float) -> list[tuple[str, TieSite, str]]:
    """Допустимые точки: бок хребта на плитах, дальний торец, бок ЦП со стороны источника."""
    supports = preview_support_slabs(survey, plate)
    occupied = _occupied_u(survey, medium)
    out = []
    # Центральная плита — только бок со стороны внешнего элемента, торцы запрещены.
    for site in central_side_sites(plate, survey.lanes, medium):
        # сторона: портал только к грани, смотрящей на source_v
        mid_v = (site.v_min + site.v_max) / 2
        if (source_v - mid_v) * (1 if source_v >= mid_v else -1) < 0:
            continue
        out.append(('central', site, f'virtual:plate:side:{medium}:{site.point.u:.3f}'))
    # Хребет — боковые точки на опорных плитах + дальний торец.
    for site in enumerate_tie_sites(
            survey.trunk_segments, supports, plate, occupied,
            required_phase=None, medium=medium):
        if site.kind == 'side':
            if not object_on_trunk_external_side(source_v, site.point.v, survey.trunk_side):
                continue
            # Не ближе BRANCH_CLEARANCE к осевым тройникам
            if any(abs(site.point.u - u) < BRANCH_CLEARANCE - 1e-7 for u in occupied):
                continue
            out.append(('trunk_side', site,
                        f'base:trunk:{medium}:side:{site.point.u:.3f}'))
        else:
            out.append(('trunk_end', site,
                        f'base:trunk:{medium}:end:{site.point.u:.3f}'))
    # Уникальность по координате
    uniq = {}
    for kind, site, key in out:
        uniq[(kind, round(site.point.u, 3), round(site.point.v, 3))] = (kind, site, key)
    return list(uniq.values())


def _exit_starts(survey: Survey, el: Element, medium: str):
    """Стартовые порты внешнего элемента."""
    if el.kind == 'tie':
        return [('point', el.center, el.center)]
    wp = next(w for w in survey.wells if w.well.number == el.well_number)
    if wp.well.exit_direction != 'auto':
        directions = (wp.well.exit_direction,)
    elif wp.well.rotation_locked or not survey.project.auto_rotate:
        directions = ('v+', 'v-') if wp.orientation == 'parallel' else ('u+', 'u-')
    else:
        directions = DIRS
    starts = []
    for direction in directions:
        orient = 'parallel' if direction.startswith('v') else 'perpendicular'
        if wp.well.rotation_locked and orient != wp.orientation:
            continue
        trial = replace(wp, well=replace(wp.well, exit_direction=direction),
                        orientation=orient)
        outlets = well_medium_outlets(trial, survey.trunk_side, survey.center,
                                      survey.project.lane_gap)
        if medium not in outlets:
            continue
        start = outlets[medium]
        normal = well_connection_normal(trial, survey.trunk_side, survey.center)
        lead = Pt(start.u + normal.u * OFFAXIS_MIN_STRAIGHT,
                  start.v + normal.v * OFFAXIS_MIN_STRAIGHT)
        starts.append((direction, start, lead))
    return starts


def _path_elbows(path):
    return sum(1 for a, b, c in zip(path, path[1:], path[2:])
               if (abs(b.u - a.u) > 1e-7 and abs(c.v - b.v) > 1e-7) or
                  (abs(b.v - a.v) > 1e-7 and abs(c.u - b.u) > 1e-7))


def _path_length(path):
    return sum(abs(b.u - a.u) + abs(b.v - a.v) for a, b in zip(path, path[1:]))


def _manhattan(a: Pt, b: Pt):
    return abs(a.u - b.u) + abs(a.v - b.v)


def _candidate_mids(lead: Pt, gate: Pt, survey: Survey):
    """Промежуточные станции обхода (ограниченный набор — скорость «Рассчитать»)."""
    mids = []
    for frac in (0.35, 0.65):
        mids.append(Pt(lead.u + (gate.u - lead.u) * frac, lead.v))
        mids.append(Pt(lead.u, lead.v + (gate.v - lead.v) * frac))
    # Редкие узлы сетки 2 м по коридору lead–gate
    lo_u, hi_u = sorted((lead.u, gate.u))
    lo_v, hi_v = sorted((lead.v, gate.v))
    u0, u1 = int(lo_u) - 1, int(hi_u) + 2
    for u in range(u0, u1, 2):
        mids.append(Pt(float(u), lead.v))
        mids.append(Pt(float(u), gate.v))
    for v in range(int(lo_v) - 1, int(hi_v) + 2, 2):
        mids.append(Pt(lead.u, float(v)))
        mids.append(Pt(gate.u, float(v)))
    seen = set()
    out = []
    for p in mids:
        key = (round(p.u, 2), round(p.v, 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out[:24]


def _routes_to_gate(lead: Pt, gate: Pt, site_point: Pt, survey: Survey,
                    owner, zone, kind: str, trunk_side: int, site: TieSite):
    """Набор ортогональных путей lead→gate→site с отсечением нерациональных."""
    routes = []
    base = shortest_paths(lead, gate, survey.wells, owner=owner, inner_zone=zone)
    for mid in _candidate_mids(lead, gate, survey):
        for part in shortest_paths(lead, mid, survey.wells, owner=owner, inner_zone=zone):
            for part2 in shortest_paths(mid, gate, survey.wells, owner=owner, inner_zone=zone):
                base.append(_compact((*part[:-1], *part2)))
    manh = _manhattan(lead, gate) + _manhattan(gate, site_point)
    best_len = inf
    for middle in base:
        full = _compact((*middle, site_point)) if middle[-1] != site_point else _compact(middle)
        if len(full) < 2 or not no_immediate_uturn(full):
            continue
        if any(not protected_line_ok(a, b, survey.wells, owner=owner)
               for a, b in zip(full, full[1:])):
            continue
        if any(inner_trunk_parallel(a, b, zone)
               for a, b in zip(full, full[1:-1])):
            continue
        if kind == 'trunk_side':
            if not external_trunk_approach(full, site_point.v, trunk_side):
                continue
        elif kind == 'trunk_end':
            # последний участок вдоль оси U наружу
            if abs(full[-2].v - site_point.v) > 1e-5:
                continue
        elif kind == 'central':
            # подход перпендикулярен к боковой грани (по V)
            if abs(full[-1].u - full[-2].u) > 1e-5:  # last seg must be vertical-ish to side
                # side port is along U on plate pipe; approach from V direction
                if abs(full[-2].u - site_point.u) > 1e-5 or abs(full[-2].v - site_point.v) < 1e-5:
                    continue
        length = _path_length(full)
        if length > manh * IRRATIONAL_LEN + 1e-6:
            continue
        if length < best_len - 1e-9:
            best_len = length
        elif length > best_len * IRRATIONAL_LEN:
            continue
        routes.append(full)
    # Уникальные по сигнатуре, лучшие по длине/отводам
    uniq = {}
    for path in routes:
        sig = tuple((round(p.u, 4), round(p.v, 4)) for p in path)
        elbows = _path_elbows(path)
        length = _path_length(path)
        prev = uniq.get(sig)
        if prev is None or (length, elbows) < (prev[0], prev[1]):
            uniq[sig] = (length, elbows, path)
    ranked = sorted(uniq.values(), key=lambda t: (t[0], t[1]))
    return [t[2] for t in ranked[:6]]


def _make_link(el, medium, start_side, start, end, path, target, status, reason, target_kind):
    return LinkCandidate(
        el.key, target, medium, start_side, 'port', start, end, path,
        _path_length(path), _path_elbows(path), status, reason, target_kind,
    )


def search_slot(survey: Survey, el: Element, medium: str, plate_name: str, plate,
                project, *, progress=None, slot_index=0, slot_total=1) -> list[LinkCandidate]:
    """Все разумные пути элемента/среды; отсортированы по цене, до PATHS_KEPT."""
    starts = _exit_starts(survey, el, medium)
    if not starts:
        return []
    sites = connection_sites(survey, plate, medium, el.center.v)
    if not sites:
        return []
    # Прореживание точек: шаг 0,3 м ВНУТРИ типа. Нельзя смешивать
    # trunk_side/central/end — иначе бок хребта «забивает» ЦП и торец,
    # и пул врезки остаётся без продления хребта / борта плиты.
    thinned = []
    last_u_by_kind = {}
    for row in sorted(sites, key=lambda r: (r[0], r[1].point.u)):
        kind, site, _key = row
        u = site.point.u
        if kind == 'trunk_end':
            thinned.append(row)
            continue
        prev = last_u_by_kind.get(kind)
        if prev is not None and abs(u - prev) < 0.29:
            continue
        thinned.append(row)
        last_u_by_kind[kind] = u
    sites = thinned
    zone = trunk_inner_zone(
        survey.lanes, survey.trunk_side,
        min((min(s.a.u, s.b.u) for s in survey.trunk_segments), default=0.),
        survey.wells)
    owner = el.well_number
    found = []
    site_best_cost = {}
    # Сначала ближайшие точки — быстрее находим хороший ориентир отсечки
    sites_sorted = sorted(
        sites,
        key=lambda row: min(_manhattan(lead, row[1].point) for _, _, lead in starts),
    )
    n_sites = max(1, len(sites_sorted))
    for site_i, (kind, site, target_key) in enumerate(sites_sorted):
        target_key = f'{target_key}:{plate_name}'
        if kind == 'central':
            gates = candidate_gates(site, trunk_side=survey.trunk_side, source_v=el.center.v)
            target_kind = 'virtual'
            status = 'гипотеза плиты'
            reason = 'Боковой порт центральной плиты (со стороны элемента)'
        elif kind == 'trunk_end':
            direction = end_outward_sign(plate, site.point)
            gates = [Pt(site.point.u + direction * 0.65, site.point.v)]
            target_kind = 'trunk'
            status = 'базовый хребет'
            reason = 'Дальний торец хребта (противоположный ЦП)'
        else:
            gates = candidate_gates(site, trunk_side=survey.trunk_side, source_v=el.center.v)
            target_kind = 'trunk'
            status = 'базовый хребет'
            reason = f'Бок хребта на плите {site.plate_type}, шаг {STEP:g} м'
        for gate in gates:
            if not protected_line_ok(gate, site.point, survey.wells, owner=owner):
                continue
            for start_side, start, lead in starts:
                if not protected_line_ok(start, lead, survey.wells, owner=owner):
                    continue
                routes = _routes_to_gate(
                    lead, gate, site.point, survey, owner, zone, kind,
                    survey.trunk_side, site)
                for middle in routes:
                    if middle and middle[0] == lead:
                        path = _compact((start, *middle))
                    else:
                        path = _compact((start, lead, *middle))
                    if len(path) < 2 or not no_immediate_uturn(path):
                        continue
                    link = _make_link(el, medium, start_side, start, site.point, path,
                                      target_key, status, reason, target_kind)
                    cost = _link_cost(link, project)
                    key = (kind, round(site.point.u, 3))
                    best = site_best_cost.get(key)
                    if best is not None and cost > best * IRRATIONAL_COST:
                        continue
                    if best is None or cost < best:
                        site_best_cost[key] = cost
                    found.append(link)
                    if progress and len(found) % 5 == 1:
                        progress('route_candidate', source=el.key, medium=medium,
                                 candidate=len(found),
                                 slot_index=slot_index, slot_total=slot_total,
                                 slot_frac=(site_i + 0.5) / n_sites,
                                 path=[(p.u, p.v) for p in path[:8]])
    # Уникальные по геометрии, топ по цене. pool[0] ОБЯЗАН остаться
    # самым дешёвым независимым путём. Дополнительно добираем квоту по
    # типу точки (side/end/ЦП), иначе врезка оставляет только side у u≈tie.
    ranked = sorted(found, key=lambda q: (_link_cost(q, project), q.elbows, q.length, q.target))

    def _kind_of(q):
        t = q.target or ''
        if ':end:' in t:
            return 'trunk_end'
        if 'plate:side' in t or 'virtual:plate' in t:
            return 'central'
        if 'trunk' in t:
            return 'trunk_side'
        return 'other'

    uniq = []
    seen = set()
    for q in ranked:
        sig = link_signature(q)
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(q)
        if len(uniq) >= PATHS_KEPT:
            break

    # Добор недостающих типов: вставляем в хвост, вытесняя лишние side.
    per_kind = max(3, PATHS_KEPT // 8)
    present = {}
    for q in uniq:
        present[_kind_of(q)] = present.get(_kind_of(q), 0) + 1
    by_kind = {}
    for q in ranked:
        by_kind.setdefault(_kind_of(q), []).append(q)
    extras = []
    for kind, rows in by_kind.items():
        have = present.get(kind, 0)
        for q in rows:
            if have >= per_kind:
                break
            sig = link_signature(q)
            if sig in seen:
                continue
            seen.add(sig)
            extras.append(q)
            have += 1
    if extras:
        # Оставляем самые дешёвые + обязательные extras редких типов.
        keep_head = max(1, PATHS_KEPT - len(extras))
        uniq = uniq[:keep_head] + extras
        if len(uniq) > PATHS_KEPT:
            uniq = uniq[:PATHS_KEPT]
    return uniq


def uniform_plates_for_path(path, medium: str, phase: str = 'active') -> list[Slab]:
    """Равномерные плиты 1,2×3 вдоль пути: зазор 3 м (допуск до 6 м)."""
    if len(path) < 2:
        return []
    # Полилайн → дуговая длина
    edges = []
    for a, b in zip(path, path[1:]):
        length = abs(a.u - b.u) + abs(a.v - b.v)
        if length < 1e-7:
            continue
        edges.append((a, b, length))
    total = sum(e[2] for e in edges)
    if total < EXT_PLATE_LEN * 0.5:
        return []
    # Центры плит: первая после EXT_LEAD, далее шаг PLATE_LEN+GAP
    step = EXT_PLATE_LEN + EXT_PLATE_GAP
    positions = []
    s = min(EXT_LEAD + EXT_PLATE_LEN / 2, max(EXT_PLATE_LEN / 2, total / 2))
    while s + EXT_PLATE_LEN / 2 <= total + 1e-6:
        positions.append(s)
        s += step
    # Если последняя далеко от конца — подтянуть ещё одну, если зазор ≤ max
    if positions:
        remain = total - (positions[-1] + EXT_PLATE_LEN / 2)
        if remain >= EXT_PLATE_GAP and remain <= EXT_PLATE_GAP_MAX + EXT_PLATE_LEN:
            positions.append(total - EXT_PLATE_LEN / 2)
    else:
        positions.append(total / 2)

    def point_at(dist):
        left = dist
        for a, b, length in edges:
            if left <= length + 1e-9:
                t = 0 if length < 1e-9 else left / length
                return Pt(a.u + (b.u - a.u) * t, a.v + (b.v - a.v) * t), a, b
            left -= length
        a, b, _ = edges[-1]
        return b, a, b

    slabs = []
    for dist in positions:
        mid, a, b = point_at(max(0.0, min(total, dist)))
        horizontal = abs(a.v - b.v) < 1e-7
        half_l = EXT_PLATE_LEN / 2
        half_w = EXT_PLATE_WID / 2
        if horizontal:
            # плита вдоль U
            u0, u1 = mid.u - half_l, mid.u + half_l
            # не выходить за сегмент грубо — ок для эскиза
            slabs.append(Slab(u0, u1, mid.v - half_w, mid.v + half_w,
                              phase, 1, (medium,), 'external_transit_run'))
        else:
            v0, v1 = mid.v - half_l, mid.v + half_l
            slabs.append(Slab(mid.u - half_w, mid.u + half_w, v0, v1,
                              phase, 1, (medium,), 'external_transit_run'))
    return slabs


def _purpose(q: LinkCandidate) -> str:
    if q.source == 'tie':
        # Подключение к хребту = продление продуктовой нитки хребта до врезки,
        # а не отдельный «внешний» отвод с плитами 1,2×3.
        t = q.target or ''
        if 'trunk' in t or q.target_kind == 'trunk':
            return 'Перспективное продление хребта'
        return 'Продуктовый нефтепровод: от центра к врезке'
    if q.source.startswith('well:'):
        return f'Отвод от скв. {q.source.split(":")[1]}'
    return 'Внешняя связь'


def _segments_for(q: LinkCandidate, phase: str = 'active'):
    return [Segment(a, b, q.medium, phase, _purpose(q))
            for a, b in zip(q.path, q.path[1:])
            if abs(a.u - b.u) + abs(a.v - b.v) > 1e-7]


def _unique_pipe_m(links) -> float:
    from pipe_render import make_network
    segs = []
    for q in links:
        segs.extend(_segments_for(q))
    total = 0.0
    for _medium, edges in make_network(segs).items():
        for a, b, _phase in edges:
            total += abs(a[0] - b[0]) + abs(a[1] - b[1])
    return total


def _unique_plate_count(links) -> int:
    """Плиты всех линий; совпадающая геометрия считается один раз."""
    boxes = set()
    for q in links:
        for slab in uniform_plates_for_path(q.path, q.medium):
            boxes.add((round(slab.u_start, 3), round(slab.u_end, 3),
                       round(slab.v_min, 3), round(slab.v_max, 3)))
    return len(boxes)


def _combo_elbows(links) -> int:
    """Грубая оценка отводов по уникальным изломам путей (без двойного счёта узлов)."""
    nodes = set()
    elbows = 0
    for q in links:
        for a, b, c in zip(q.path, q.path[1:], q.path[2:]):
            turn = ((abs(b.u - a.u) > 1e-7 and abs(c.v - b.v) > 1e-7) or
                    (abs(b.v - a.v) > 1e-7 and abs(c.u - b.u) > 1e-7))
            if not turn:
                continue
            key = (q.medium, round(b.u, 4), round(b.v, 4))
            if key in nodes:
                continue
            nodes.add(key)
            elbows += 1
    return elbows


def combo_external_cost(links, project) -> float:
    """Стоимость набора внешних связей с разделением общих труб и плит."""
    if not links:
        return 0.0
    pipe = _unique_pipe_m(links) * project.pipe_cost_per_m
    elbows = _combo_elbows(links) * project.elbow_cost
    # тройники при слиянии одной среды: число источников − число уникальных концов
    by_med = defaultdict(list)
    for q in links:
        by_med[q.medium].append(q)
    tees = 0
    for medium, rows in by_med.items():
        ends = {(round(q.end.u, 3), round(q.end.v, 3)) for q in rows}
        if len(rows) > 1:
            tees += max(0, len(rows) - len(ends))
    tees_cost = tees * project.tee_cost
    plates = _unique_plate_count(links) * project.plate_cost
    return pipe + elbows + tees_cost + plates


def attach_external_independent(core_result, project, survey=None, *, progress=None):
    """После осевого ядра: независимые лучшие внешние трассы + пул вариантов."""
    survey = survey or make_survey(project)
    plate = core_result.central_plate
    if plate is None:
        raise ValueError('Нет центральной плиты для внешних подключений')
    plate_name = 'right' if core_result.selected_center_position.startswith('right') else 'left'
    slots = external_slots(survey)
    pool = {}          # key -> list[LinkCandidate]
    chosen = []        # best per slot
    failures = []
    n_slots = max(1, len(slots))
    for slot_i, (el, medium) in enumerate(slots):
        if progress:
            progress('stage', message=f'Внешнее [{slot_i+1}/{len(slots)}]: {el.label} · {medium}',
                     slot_index=slot_i, slot_total=n_slots, slot_frac=0.0)
        variants = search_slot(survey, el, medium, plate_name, plate, project,
                               progress=progress, slot_index=slot_i, slot_total=n_slots)
        pool[f'{el.key}|{medium}'] = variants
        if not variants:
            failures.append(f'{el.label}/{medium}: нет допустимой точки подключения')
            continue
        chosen.append(variants[0])
        if progress:
            progress('source_done', source=el.key, medium=medium,
                     candidate=len(variants),
                     slot_index=slot_i, slot_total=n_slots, slot_frac=1.0)
    # Оверлей на результат ядра (линии независимы, могут пересекаться)
    result = deepcopy(core_result)
    ext_segs = []
    ext_slabs = []
    indep_cost = 0.0
    for q in chosen:
        phase = 'active'
        if q.source.startswith('well:'):
            wp = next((w for w in survey.wells if w.well.number == int(q.source.split(':')[1])), None)
            if wp is not None:
                phase = line_phase(wp, q.medium)
        ext_segs.extend(_segments_for(q, phase))
        ext_slabs.extend(uniform_plates_for_path(q.path, q.medium, phase))
        indep_cost += _link_cost(q, project)
        indep_cost += len(uniform_plates_for_path(q.path, q.medium, phase)) * project.plate_cost
    result.segments = list(result.segments) + ext_segs
    result.slabs = list(result.slabs) + ext_slabs
    core_score = float(result.metrics.get('optimized_score', estimate_score(core_result)))
    result.metrics['external_independent_cost'] = float(indep_cost)
    result.metrics['external_slots'] = float(len(slots))
    result.metrics['external_connected'] = float(len(chosen))
    result.metrics['external_path_pool'] = float(sum(len(v) for v in pool.values()))
    result.metrics['optimized_score'] = float(core_score + indep_cost)
    result.metrics['small_plate_count'] = float(count_small_plates(result.slabs))
    result.decision_trace['_external_independent'] = {
        'mode': 'independent_external_v92',
        'plate': plate_name,
        'slots': [f'{el.key}|{m}' for el, m in slots],
        'chosen': [
            {'source': q.source, 'medium': q.medium, 'target': q.target,
             'length': round(q.length, 4), 'elbows': int(q.elbows),
             'cost': round(_link_cost(q, project), 2),
             'path': [(round(p.u, 4), round(p.v, 4)) for p in q.path]}
            for q in chosen
        ],
        'pool_counts': {k: len(v) for k, v in pool.items()},
        'failures': failures,
        'rules': {
            'trunk': 'side_on_trunk_plates_or_far_end_only_external_side',
            'central_plate': 'side_facing_element_only_no_ends',
            'plates': f'uniform_1.2x3_gap_{EXT_PLATE_GAP:g}_to_{EXT_PLATE_GAP_MAX:g}',
            'independence': 'lines_ignore_each_other',
        },
    }
    # Пул в каталог для Optimize / снимка
    ext_catalog = {}
    for key, variants in pool.items():
        source = key.split('|', 1)[0]
        ext_catalog.setdefault(source, [])
        ext_catalog[source].extend(variants)
    result.decision_trace['_external_pool_hashes'] = {
        k: [link_signature(q) for q in variants] for k, variants in pool.items()
    }
    notes = [
        f'Внешние (независимо): подключено {len(chosen)}/{len(slots)} слотов; '
        f'запомнено вариантов {sum(len(v) for v in pool.values())}; '
        f'стоимость внешних {indep_cost:,.0f} ₽ (без деления плит/труб).',
        'Линии и плиты внешних элементов на этом этапе независимы и могут пересекаться.',
    ]
    if failures:
        notes.append('Не подключены: ' + '; '.join(failures[:5]))
    result.notes = [n for n in result.notes
                    if 'ВНЕШНИЕ ПОДКЛЮЧЕНИЯ ОТКЛЮЧЕНЫ' not in n] + notes
    result.metrics['core_mode'] = 0.0
    result.metrics['external_mode'] = 1.0
    return result, ext_catalog, pool, {
        'mode': 'independent_external_v92',
        'connected': len(chosen),
        'slots': len(slots),
        'pool_sizes': {k: len(v) for k, v in pool.items()},
    }


def optimize_external_combinations(project, survey, core_result, pool, *,
                                   progress=None, limit=80):
    """Совместимость: алгоритм вынесен в optimize_algorithm.py."""
    from optimize_algorithm import run_optimization_search
    result, _report = run_optimization_search(
        project, survey, core_result, pool, progress=progress, limit=limit)
    return result
