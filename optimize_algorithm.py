"""Алгоритм кнопки «Оптимизировать».

После «Рассчитать» внешние линии независимы. Здесь строим ТРАНЗИТНУЮ
МАГИСТРАЛЬ: разные среды на параллельных осях с шагом 0,30 м, общие
плиты 1,2×3 оплачиваются один раз.

Правило результата:
  • либо независимая линия (своя ось, свои плиты);
  • либо участник магистрального пучка (оси 0,30 м, общие плиты);
  • совпадение разных сред на ОДНОЙ оси запрещено.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from itertools import combinations

from bundle_geometry import AXIS_GAP, TRANSIT_WIDTH, fits
from catalog_solver import _link_cost, link_signature
from network_cost import count_small_plates
from planning_workflow import LinkCandidate, make_survey
from routing import Pt

from external_routes import (
    PATHS_KEPT,
    _path_elbows,
    _path_length,
    _segments_for,
    uniform_plates_for_path,
)

EXT_ALONG_MATCH = 1.6
PLATE_LEN = 3.0
PLATE_WID = 1.2
PLATE_GAP = 3.0
# Два параллельных ряда плит «почти вплотную» (центры ~1,2 м) —
# юридически на одну плиту 1,2 м не влезают, но сдвиг до 0,30 м обычно
# дешевле двух рядов. Ищем такие пары и стягиваем в пучок.
# До ~6 м: на чертеже «два ряда рядом» (дефолт 9/10 + врезка ~4,5 м).
NEAR_PARALLEL_MAX_M = 6.0
NEAR_PARALLEL_OVERLAP_M = 3.0


# ---------------------------------------------------------------------------
# Геометрия пути / оси
# ---------------------------------------------------------------------------

def _dominant_vertical_u(q: LinkCandidate):
    best_u, best_len = None, 0.0
    for a, b in zip(q.path, q.path[1:]):
        if abs(a.u - b.u) > 1e-7:
            continue
        length = abs(a.v - b.v)
        if length > best_len:
            best_len, best_u = length, a.u
    return best_u


def _vertical_span(q: LinkCandidate):
    """(u, v_lo, v_hi) самой длинной вертикали или None."""
    best = None
    best_len = 0.0
    for a, b in zip(q.path, q.path[1:]):
        if abs(a.u - b.u) > 1e-7:
            continue
        lo, hi = sorted((a.v, b.v))
        length = hi - lo
        if length > best_len:
            best_len = length
            best = (a.u, lo, hi)
    return best


def _compact_path(points):
    out = []
    for p in points:
        if not out:
            out.append(p)
            continue
        prev = out[-1]
        if abs(prev.u - p.u) < 1e-7 and abs(prev.v - p.v) < 1e-7:
            continue
        if len(out) >= 2:
            a = out[-2]
            col = (abs(a.u - prev.u) < 1e-7 and abs(prev.u - p.u) < 1e-7)
            row = (abs(a.v - prev.v) < 1e-7 and abs(prev.v - p.v) < 1e-7)
            if col or row:
                out[-1] = p
                continue
        out.append(p)
    return tuple(out)


def _target_for(q: LinkCandidate, new_u: float) -> str:
    t = q.target or ''
    if 'trunk' in t or q.target_kind == 'trunk':
        kind = 'end' if ':end:' in t else 'side'
        return f'base:trunk:{q.medium}:{kind}:{new_u:.3f}:magistral'
    return f'virtual:plate:side:{q.medium}:{new_u:.3f}:magistral'


def rewrite_to_axis(q: LinkCandidate, new_u: float) -> LinkCandidate:
    """Ортогональный путь: выход → ось new_u → порт среды (end.v)."""
    start = q.start
    end = Pt(float(new_u), float(q.end.v))
    pts = [start]
    lead = start
    if len(q.path) >= 2:
        stub = q.path[1]
        if abs(stub.u - start.u) + abs(stub.v - start.v) <= 8.0 + 1e-6:
            pts.append(stub)
            lead = stub
    if abs(lead.u - new_u) > 1e-6:
        pts.append(Pt(new_u, lead.v))
    if abs(pts[-1].v - end.v) > 1e-6 or abs(pts[-1].u - end.u) > 1e-6:
        pts.append(end)
    path = _compact_path(pts)
    return LinkCandidate(
        q.source, _target_for(q, new_u), q.medium, q.start_side, q.end_side,
        start, end, path, _path_length(path), _path_elbows(path),
        'ok', 'Транзитная магистраль (общие плиты 1,2×3)', q.target_kind,
    )


def pack_offsets(media, center_u, gap=AXIS_GAP):
    ordered = sorted(set(media))
    n = len(ordered)
    if n == 0:
        return {}
    if n == 1:
        return {ordered[0]: center_u}
    half = (n - 1) / 2.0
    return {m: center_u + (i - half) * gap for i, m in enumerate(ordered)}


def build_magistral(links, center_u):
    """Собрать пучок: разные среды на осях center±k·0,30 м."""
    if len(links) < 2:
        return None
    media = [q.medium for q in links]
    if len(set(media)) < 2:
        return None
    offsets = pack_offsets(media, center_u)
    axes = [(u, m) for m, u in offsets.items()]
    if not fits(axes):
        return None
    out = []
    for q in links:
        out.append(rewrite_to_axis(q, offsets[q.medium]))
    # запрет: две среды на одной оси
    by_med = {}
    for q in out:
        u = _dominant_vertical_u(q)
        if u is None:
            return None
        if q.medium in by_med and abs(by_med[q.medium] - u) > 1e-3:
            return None
        by_med[q.medium] = u
    vals = list(by_med.values())
    if max(vals) - min(vals) < AXIS_GAP - 1e-6 and len(by_med) >= 2:
        return None
    if max(vals) - min(vals) > TRANSIT_WIDTH + 1e-6:
        return None
    return out


# ---------------------------------------------------------------------------
# Смета: трубы / отводы / плиты с учётом пучка
# ---------------------------------------------------------------------------

def unique_pipe_m(links) -> float:
    from pipe_render import make_network
    segs = []
    for q in links:
        segs.extend(_segments_for(q))
    total = 0.0
    for _medium, edges in make_network(segs).items():
        for a, b, _phase in edges:
            total += abs(a[0] - b[0]) + abs(a[1] - b[1])
    return total


def combo_elbows(links) -> int:
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


def _cluster_vertical_packs(links):
    """Группы параллельных вертикалей, влезающих на плиту 1,2 м."""
    runs = []  # (link_index, u, v_lo, v_hi, medium)
    for i, q in enumerate(links):
        span = _vertical_span(q)
        if not span:
            continue
        u, lo, hi = span
        if hi - lo < 2.5:
            continue
        runs.append((i, u, lo, hi, q.medium))
    packs = []
    used = [False] * len(runs)
    for i, run in enumerate(runs):
        if used[i]:
            continue
        group = [run]
        used[i] = True
        for j in range(i + 1, len(runs)):
            if used[j]:
                continue
            other = runs[j]
            # одна среда — только точная общая ось (труба один раз уже в pipe)
            trial = group + [other]
            by_med = {}
            ok = True
            for _i, u, _lo, _hi, med in trial:
                if med in by_med and abs(by_med[med] - u) > 1e-3:
                    ok = False
                    break
                by_med[med] = u
            if not ok:
                continue
            axes = list(by_med.items())  # med -> u  need (u, med)
            axis_list = [(u, m) for m, u in by_med.items()]
            if len(by_med) >= 2:
                us = [u for u, _m in axis_list]
                if max(us) - min(us) < AXIS_GAP - 1e-6:
                    continue
                if not fits(axis_list):
                    continue
            elif abs(run[1] - other[1]) > 1e-3:
                continue
            group.append(other)
            used[j] = True
        packs.append(group)
    return packs


def _shared_vertical_plate_count(pack) -> int:
    """Один ряд плит 1,2×3 на ОБЪЕДИНЕНИИ вертикалей пучка.

    Раньше считали только пересечение + «частные хвосты» по каждой среде —
    из‑за этого полный пучок 9+10+врезка выходил дороже частичного 9+врезка
    (второй ряд у 10), хотя физически магистраль — один коридор плит.
    """
    if not pack:
        return 0
    v_lo = min(r[2] for r in pack)
    v_hi = max(r[3] for r in pack)
    length = v_hi - v_lo
    if length < PLATE_LEN * 0.5:
        return 0
    step = PLATE_LEN + PLATE_GAP
    n = 0
    s = v_lo + PLATE_LEN / 2
    while s + PLATE_LEN / 2 <= v_hi + 1e-6:
        n += 1
        s += step
    return n if n else 1


def _horizontal_plate_count(links) -> int:
    """Плиты только на горизонтальных участках (подход к оси)."""
    count = 0
    seen = set()
    for q in links:
        for a, b in zip(q.path, q.path[1:]):
            if abs(a.v - b.v) > 1e-7:
                continue  # не горизонталь
            length = abs(a.u - b.u)
            if length < PLATE_LEN * 0.5:
                continue
            # грубая уникальность по коробке
            lo, hi = sorted((a.u, b.u))
            key = (round(a.v, 2), round(lo, 2), round(hi, 2), q.medium)
            if key in seen:
                continue
            # соседние среды на той же горизонтали с зазором пучка — одна плита
            merged = False
            for ov, olo, ohi, omed in list(seen):
                if abs(ov - round(a.v, 2)) > 1e-3:
                    continue
                if min(hi, ohi) - max(lo, olo) < PLATE_LEN * 0.5:
                    continue
                # поперечный разнос осей здесь по V одинаков; для горизонтали
                # cross = v, уже совпал — разные среды на одной полке v:
                if omed != q.medium:
                    merged = True
                    break
            seen.add(key)
            if not merged:
                n = max(1, int((length + PLATE_GAP) // (PLATE_LEN + PLATE_GAP)))
                count += n
    return count


def unique_plate_count(links) -> int:
    """Плиты с общей сеткой на транзитной магистрали."""
    packs = _cluster_vertical_packs(links)
    vertical = sum(_shared_vertical_plate_count(p) for p in packs)
    # горизонтальные подходы — по сегментам, без двойного счёта вертикалей
    horizontal = _horizontal_plate_count(links)
    return vertical + horizontal

def combo_external_cost(links, project) -> float:
    if not links:
        return 0.0
    pipe = unique_pipe_m(links) * project.pipe_cost_per_m
    elbows = combo_elbows(links) * project.elbow_cost
    by_med = defaultdict(list)
    for q in links:
        by_med[q.medium].append(q)
    tees = 0
    for _medium, rows in by_med.items():
        ends = {(round(q.end.u, 3), round(q.end.v, 3)) for q in rows}
        if len(rows) > 1:
            tees += max(0, len(rows) - len(ends))
    plates = unique_plate_count(links) * project.plate_cost
    return pipe + elbows + tees * project.tee_cost + plates


def independent_cost(links, project) -> float:
    """База «Рассчитать»: каждая линия со своими плитами, без деления."""
    return sum(_link_cost(q, project) for q in links) + sum(
        len(uniform_plates_for_path(q.path, q.medium)) * project.plate_cost
        for q in links)


# ---------------------------------------------------------------------------
# Поиск магистралей
# ---------------------------------------------------------------------------

def _anchor_candidates(links):
    """Центры пучка: середины пар (макс. экономия), врезка, скважины."""
    anchors = []
    us = []
    for q in links:
        u = _dominant_vertical_u(q)
        if u is None:
            continue
        span = _vertical_span(q)
        weight = (span[2] - span[1]) if span else 1.0
        us.append((u, weight, q))
    # 1) взвешенные середины пар — обычно лучшая экономия труб+плит
    for i, (u1, w1, _a) in enumerate(us):
        for u2, w2, _b in us[i + 1:]:
            anchors.append((w1 * u1 + w2 * u2) / (w1 + w2))
            anchors.append(0.5 * (u1 + u2))
            # третьи точки
            anchors.append(u1 + (u2 - u1) / 3)
            anchors.append(u1 + 2 * (u2 - u1) / 3)
    # 2) врезка
    for u, _w, q in us:
        if q.source == 'tie' or q.medium == 'oil_product':
            anchors.extend([u, u - AXIS_GAP, u + AXIS_GAP])
    # 3) скважины
    for u, _w, q in us:
        if q.source.startswith('well:'):
            anchors.extend([u, u - AXIS_GAP, u + AXIS_GAP])
    out = []
    for a in anchors:
        if any(abs(a - x) < 1e-3 for x in out):
            continue
        out.append(a)
    return out


def near_parallel_groups(links):
    """Пары/группы вертикалей с перекрытием и разносом до NEAR_PARALLEL_MAX_M.

    Типичный баг на чертеже: magenta врезка и соседняя нитка идут почти
    рядом, у каждой свой ряд плит 1,2×3. На одну плиту при текущем разносе
    не влезают (span>~1,0 м), но стянуть до 0,30 м почти всегда дешевле.

    Важно: скважины 9 и 10 после «Рассчитать» часто уже на ОДНОЙ оси
    (sep≈0), а врезка — в 4–5 м. Их нельзя терять из группы из‑за
    `sep < AXIS_GAP`: иначе nudge стягивает только 9+врезку и оставляет
    второй ряд плит у 10.
    """
    rows = []
    for i, q in enumerate(links):
        span = _vertical_span(q)
        if not span:
            continue
        u, lo, hi = span
        if hi - lo < NEAR_PARALLEL_OVERLAP_M:
            continue
        rows.append((i, q, u, lo, hi))
    groups = []
    used = set()
    for a, (i, q, u, lo, hi) in enumerate(rows):
        if i in used:
            continue
        group = [(i, q, u, lo, hi)]
        used.add(i)
        # Расширяем группу, пока находятся новые участники (в т.ч. соседи
        # с sep≈0 на уже собранной оси).
        changed = True
        while changed:
            changed = False
            g_us = [g[2] for g in group]
            g_lo = min(g[3] for g in group)
            g_hi = max(g[4] for g in group)
            g_media = {g[1].medium for g in group}
            for b in range(len(rows)):
                j, q2, u2, lo2, hi2 = rows[b]
                if j in used:
                    continue
                overlap = min(g_hi, hi2) - max(g_lo, lo2)
                if overlap < NEAR_PARALLEL_OVERLAP_M:
                    continue
                sep = min(abs(u2 - gu) for gu in g_us)
                if sep > NEAR_PARALLEL_MAX_M + 1e-6:
                    continue
                # Сосед на той же оси (другая среда) — всегда в пучок.
                same_axis = sep < 1e-3
                if same_axis and q2.medium in g_media:
                    continue
                if not same_axis and sep < AXIS_GAP - 1e-6:
                    continue
                if not same_axis and q2.medium in g_media and sep > 1e-3:
                    continue
                if len(g_media | {q2.medium}) < 2 and not same_axis:
                    continue
                group.append((j, q2, u2, lo2, hi2))
                used.add(j)
                changed = True
        if len({g[1].medium for g in group}) >= 2 and len(group) >= 2:
            groups.append(group)
    return groups


def nudge_near_parallel_proposals(all_links, project):
    """Стянуть близкие параллели до пучка 0,30 м (общие плиты)."""
    if not getattr(project, 'shared_corridors', True):
        return []
    out = []
    for group in near_parallel_groups(all_links):
        us = [g[2] for g in group]
        center = sum(us) / len(us)
        subset = [g[1] for g in group]
        # несколько якорей вокруг текущего центра — минимальный сдвиг
        for c in (center, min(us) + AXIS_GAP, max(us) - AXIS_GAP,
                  center - AXIS_GAP / 2, center + AXIS_GAP / 2):
            packed = build_magistral(subset, c)
            if not packed:
                continue
            by_src = {q.source: q for q in packed}
            trial = [by_src.get(q.source, q) for q in all_links]
            tag = (f'nudge[{",".join(q.source for q in subset)}]'
                   f'@{c:.2f}')
            out.append((tag, trial))
    return out


def magistral_proposals(all_links, project):
    """Кандидаты: полный пучок и пучки «врезка + каждая скважина»."""
    if not getattr(project, 'shared_corridors', True):
        return []
    if len(all_links) < 2:
        return []

    proposals = []
    seen = set()

    def _add(subset, center, tag):
        built = build_magistral(subset, center)
        if not built:
            return
        # полный набор слотов: магистральные заменяют свои source
        by_src = {q.source: q for q in built}
        trial = [by_src.get(q.source, q) for q in all_links]
        key = tuple(sorted(
            (q.source, q.medium, round(_dominant_vertical_u(q) or q.end.u, 3))
            for q in trial))
        if key in seen:
            return
        seen.add(key)
        proposals.append((tag, trial))

    anchors = _anchor_candidates(all_links)
    tie = [q for q in all_links if q.source == 'tie' or q.medium == 'oil_product']
    wells = [q for q in all_links if q.source.startswith('well:')]

    # A) полный пучок всех внешних
    for center in anchors[:40]:
        _add(all_links, center, f'full@{center:.2f}')

    # B) врезка + одна скважина (ключевой кейс: 10 + точка врезки)
    if tie:
        for w in wells:
            subset = [tie[0], w]
            for center in _anchor_candidates(subset)[:20]:
                _add(subset, center, f'{w.source}+tie@{center:.2f}')

    # C) врезка + все скважины
    if tie and len(wells) >= 2:
        subset = [tie[0], *wells]
        for center in _anchor_candidates(subset)[:20]:
            _add(subset, center, f'wells+tie@{center:.2f}')

    # D) только скважины между собой
    if len(wells) >= 2:
        for center in _anchor_candidates(wells)[:16]:
            _add(wells, center, f'wells@{center:.2f}')

    return proposals


def bundled_plates_for_links(links):
    """Плиты для чертежа: один ряд на объединении вертикалей пучка."""
    from routing import Slab
    slabs = []
    packs = _cluster_vertical_packs(links)
    covered_link_idx = set()
    for pack in packs:
        media = tuple(sorted({r[4] for r in pack}))
        us = [r[1] for r in pack]
        cross = (min(us) + max(us)) / 2
        half_w = PLATE_WID / 2
        half_l = PLATE_LEN / 2
        # объединение по V — один коридор, без частных хвостов по средам
        v_lo = min(r[2] for r in pack)
        v_hi = max(r[3] for r in pack)
        for i, *_rest in pack:
            covered_link_idx.add(i)
        positions = []
        if v_hi - v_lo >= PLATE_LEN * 0.5:
            step = PLATE_LEN + PLATE_GAP
            s = v_lo + PLATE_LEN / 2
            while s + PLATE_LEN / 2 <= v_hi + 1e-6:
                positions.append(s)
                s += step
            if not positions:
                positions.append(0.5 * (v_lo + v_hi))
        for mid in positions:
            slabs.append(Slab(cross - half_w, cross + half_w,
                              mid - half_l, mid + half_l,
                              'active', len(media), media, 'external_transit_run'))
    # горизонтали + вертикали вне пучков — стандартная раскладка
    for i, q in enumerate(links):
        if i in covered_link_idx:
            # только горизонтальные куски
            for a, b in zip(q.path, q.path[1:]):
                if abs(a.v - b.v) > 1e-7:
                    continue
                length = abs(a.u - b.u)
                if length < PLATE_LEN * 0.5:
                    continue
                path = (a, b)
                for slab in uniform_plates_for_path(path, q.medium):
                    slabs.append(slab)
        else:
            slabs.extend(uniform_plates_for_path(q.path, q.medium))
    # уникальность по ключу коробки
    boxes = {}
    for slab in slabs:
        key = (round(slab.u_start, 3), round(slab.u_end, 3),
               round(slab.v_min, 3), round(slab.v_max, 3))
        if key not in boxes:
            boxes[key] = slab
        else:
            old = boxes[key]
            media = tuple(sorted(set(old.media) | set(slab.media)))
            boxes[key] = replace(old, media=media, media_count=len(media))
    return list(boxes.values())


# ---------------------------------------------------------------------------
# Пул / вход
# ---------------------------------------------------------------------------

def rebuild_pool_from_catalog(catalog, initial, project):
    pool = {}
    bysig = {link_signature(q): q for rows in (catalog or {}).values() for q in rows}
    for key, hashes in (initial.decision_trace.get('_external_pool_hashes') or {}).items():
        variants = []
        for sig in hashes:
            q = bysig.get(sig)
            if q is not None:
                variants.append(q)
        if not variants:
            source, medium = key.split('|', 1)
            ranked = sorted(
                (q for q in catalog.get(source, ()) if q.medium == medium and q.path),
                key=lambda q: (_link_cost(q, project), q.elbows, q.length))
            variants = ranked[:PATHS_KEPT]
        if variants:
            pool[key] = variants
    return pool


# ---------------------------------------------------------------------------
# Ядро оптимизации
# ---------------------------------------------------------------------------

def run_optimization_search(project, survey, core_result, pool, *,
                            progress=None, limit=160):
    survey = survey or make_survey(project)
    slots = list(pool.keys())
    report = {
        'mode': 'transit_magistral_v93d_one_row',
        'slots': slots,
        'pool_counts': {k: len(v) for k, v in pool.items()},
        'trials': 0,
        'improved': 0,
        'skipped': False,
    }
    if not slots:
        core_result.notes.append('Оптимизировать: внешних слотов нет.')
        report['skipped'] = True
        report['reason'] = 'no_slots'
        return core_result, report

    # Стартовая точка — лучшие независимые пути (pool[0] = дешевле всех)
    best_links = [pool[s][0] for s in slots if pool[s]]
    if len(best_links) != len(slots):
        raise ValueError('Оптимизировать: в пуле есть пустые слоты')

    base_indep = independent_cost(best_links, project)
    best_cost = combo_external_cost(best_links, project)
    trials = 0
    improved = 0
    trial_log = []

    def consider(trial, kind):
        nonlocal best_links, best_cost, trials, improved
        if trials >= limit:
            return False
        trials += 1
        cost = combo_external_cost(trial, project)
        if progress and trials % 3 == 0:
            progress('optimization', candidate=trials, total=limit,
                     cost=round(cost, 0), best=round(best_cost, 0))
        better = cost + 1e-6 < best_cost
        if len(trial_log) < 120:
            trial_log.append({
                'n': trials, 'kind': kind, 'cost': round(cost, 2),
                'accepted': better,
                'sources': [q.source for q in trial],
                'axes': [round(_dominant_vertical_u(q) or q.end.u, 3) for q in trial],
            })
        if better:
            best_cost = cost
            best_links = list(trial)
            improved += 1
        return True

    # === 0. Стянуть уже близкие параллели (два ряда плит рядом) ===
    # Preference: дешевле → общие плиты, если среды допускают пучок.
    for tag, trial in nudge_near_parallel_proposals(best_links, project):
        if trials >= limit:
            break
        consider(trial, f'nudge:{tag}')

    # === 1. Транзитные магистрали (синтез коридора) ===
    seed_links = [best_links]
    for i, slot in enumerate(slots):
        for alt in pool[slot][:6]:
            trial = list(best_links)
            trial[i] = alt
            seed_links.append(trial)
    seen_prop = set()
    batch = []
    for seed in seed_links[:24]:
        for tag, trial in (list(nudge_near_parallel_proposals(seed, project))
                           + list(magistral_proposals(seed, project))):
            key = tuple(sorted(
                (q.source, round(_dominant_vertical_u(q) or q.end.u, 3))
                for q in trial))
            if key in seen_prop:
                continue
            seen_prop.add(key)
            batch.append((combo_external_cost(trial, project), tag, trial))
    batch.sort(key=lambda t: t[0])
    for cost_est, tag, trial in batch:
        if trials >= limit:
            break
        consider(trial, f'magistral:{tag}')

    # === 1b. Повторный nudge по лучшему набору (если после замен снова два ряда) ===
    for tag, trial in nudge_near_parallel_proposals(best_links, project):
        if trials >= limit:
            break
        consider(trial, f'nudge_final:{tag}')

    # === 1c. Принудительный полный пучок: если ещё ≥2 ряда плит — стянуть ===
    # Preference: minimize cost → один коридор 0,30 м с общими плитами.
    def _multi_row_span(links):
        us = []
        for q in links:
            span = _vertical_span(q)
            if not span:
                continue
            _u, lo, hi = span
            if hi - lo < NEAR_PARALLEL_OVERLAP_M:
                continue
            us.append(span[0])
        if len(us) < 2:
            return 0.0
        return max(us) - min(us)

    if (_multi_row_span(best_links) > AXIS_GAP + 1e-6
            and getattr(project, 'shared_corridors', True)):
        for tag, trial in magistral_proposals(best_links, project):
            if 'full@' not in tag and 'wells+tie@' not in tag:
                continue
            if _multi_row_span(trial) > TRANSIT_WIDTH + 1e-6:
                continue
            if trials >= limit:
                # даже при исчерпанном лимите — одна попытка дешёвого пучка
                cost = combo_external_cost(trial, project)
                if cost + 1e-6 < best_cost:
                    best_cost = cost
                    best_links = list(trial)
                    improved += 1
                    trials += 1
                    trial_log.append({
                        'n': trials, 'kind': f'force_collapse:{tag}',
                        'cost': round(cost, 2), 'accepted': True,
                        'sources': [q.source for q in trial],
                        'axes': [round(_dominant_vertical_u(q) or q.end.u, 3)
                                 for q in trial],
                    })
                break
            consider(trial, f'force_collapse:{tag}')

    # === 2. Одиночные замены из пула (тонкая подстройка) ===
    for i, slot in enumerate(slots):
        if trials >= limit:
            break
        for alt in pool[slot][:12]:
            if trials >= limit:
                break
            if link_signature(alt) == link_signature(best_links[i]):
                continue
            trial = list(best_links)
            trial[i] = alt
            # после замены — сразу попробовать магистраль с новым якорем
            consider(trial, f'single:{slot}')
            for tag, packed in magistral_proposals(trial, project)[:6]:
                if trials >= limit:
                    break
                consider(packed, f'magistral_after:{tag}')

    # === 3. Пары из пула + магистраль ===
    idx = list(range(len(slots)))
    for ia, ib in list(combinations(idx, 2))[:10]:
        if trials >= limit:
            break
        for a in pool[slots[ia]][:5]:
            for b in pool[slots[ib]][:5]:
                if trials >= limit:
                    break
                trial = list(best_links)
                trial[ia] = a
                trial[ib] = b
                consider(trial, f'pair:{slots[ia]}+{slots[ib]}')
                for tag, packed in magistral_proposals(trial, project)[:4]:
                    if trials >= limit:
                        break
                    consider(packed, f'magistral_pair:{tag}')

    # === 4. Финальный collapse: после замен из пула снова могли остаться два ряда ===
    if (_multi_row_span(best_links) > AXIS_GAP + 1e-6
            and getattr(project, 'shared_corridors', True)):
        collapse = []
        for tag, trial in magistral_proposals(best_links, project):
            if 'full@' not in tag and 'wells+tie@' not in tag:
                continue
            if _multi_row_span(trial) > TRANSIT_WIDTH + 1e-6:
                continue
            collapse.append((combo_external_cost(trial, project), tag, trial))
        collapse.sort(key=lambda t: t[0])
        for cost, tag, trial in collapse[:12]:
            if cost + 1e-6 >= best_cost:
                break
            best_cost = cost
            best_links = list(trial)
            improved += 1
            trials += 1
            if len(trial_log) < 120:
                trial_log.append({
                    'n': trials, 'kind': f'final_collapse:{tag}',
                    'cost': round(cost, 2), 'accepted': True,
                    'sources': [q.source for q in trial],
                    'axes': [round(_dominant_vertical_u(q) or q.end.u, 3)
                             for q in trial],
                })
            break

    # === 5. Жёсткий коридор 0,30 м: «рыхлый» пучок (разнос ~1 м на одной
    # плите) дешевле по смете, но визуально/по правилу — два ряда.
    # Переписываем пересекающиеся вертикали через build_magistral.
    if getattr(project, 'shared_corridors', True) and len(best_links) >= 2:
        def _axis_gaps(links):
            us = sorted({round(_dominant_vertical_u(q) or q.end.u, 4)
                         for q in links if _vertical_span(q)})
            if len(us) < 2:
                return ()
            return tuple(us[i + 1] - us[i] for i in range(len(us) - 1))

        gaps = _axis_gaps(best_links)
        loose = any(g > AXIS_GAP + 0.05 for g in gaps) and (
            max(_dominant_vertical_u(q) or q.end.u for q in best_links)
            - min(_dominant_vertical_u(q) or q.end.u for q in best_links)
            <= TRANSIT_WIDTH + 1e-6)
        if loose or (_multi_row_span(best_links) > AXIS_GAP + 1e-6):
            tight = []
            for tag, trial in magistral_proposals(best_links, project):
                if 'full@' not in tag and 'wells+tie@' not in tag:
                    continue
                tg = _axis_gaps(trial)
                if not tg or max(tg) > AXIS_GAP + 0.05:
                    continue
                if _multi_row_span(trial) > (len({q.medium for q in trial}) - 1) * AXIS_GAP + 0.05:
                    continue
                tight.append((combo_external_cost(trial, project), tag, trial))
            # якоря вокруг текущего центра — минимальный сдвиг трубы
            us_now = [_dominant_vertical_u(q) for q in best_links
                      if _dominant_vertical_u(q) is not None]
            if us_now:
                mid = 0.5 * (min(us_now) + max(us_now))
                for c in (mid, min(us_now), max(us_now),
                          mid - AXIS_GAP, mid + AXIS_GAP):
                    packed = build_magistral(best_links, c)
                    if not packed:
                        continue
                    tg = _axis_gaps(packed)
                    if not tg or max(tg) > AXIS_GAP + 0.05:
                        continue
                    by = {q.source: q for q in packed}
                    trial = [by.get(q.source, q) for q in best_links]
                    tight.append((combo_external_cost(trial, project),
                                  f'tight@{c:.2f}', trial))
            tight.sort(key=lambda t: t[0])
            if tight:
                cost, tag, trial = tight[0]
                # Принимаем плотный пучок: он чуть дороже «рыхлого», но
                # это требование коридора 0,30 м / одного ряда плит.
                # Не хуже независимой базы.
                if cost <= base_indep + 1e-6:
                    best_cost = cost
                    best_links = list(trial)
                    improved += 1
                    trials += 1
                    if len(trial_log) < 120:
                        trial_log.append({
                            'n': trials, 'kind': f'tight_corridor:{tag}',
                            'cost': round(cost, 2), 'accepted': True,
                            'sources': [q.source for q in trial],
                            'axes': [round(_dominant_vertical_u(q) or q.end.u, 3)
                                     for q in trial],
                        })

    # --- Сборка результата ---
    result = deepcopy(core_result)
    axial_ids = {wp.well.number for wp in survey.wells if wp.well.on_axis}
    kept = []
    for s in core_result.segments:
        if s.purpose.startswith('Продуктовый нефтепровод'):
            continue
        if s.purpose == 'Перспективное продление хребта':
            continue
        if s.purpose.startswith('Отвод от скв.'):
            try:
                num = int(s.purpose.split('скв. ', 1)[1].split()[0])
            except Exception:
                kept.append(s)
                continue
            if num in axial_ids:
                kept.append(s)
            continue
        kept.append(s)
    result.segments = list(kept)
    result.slabs = [s for s in core_result.slabs if s.slab_type != 'external_transit_run']
    for q in best_links:
        result.segments.extend(_segments_for(q, 'active'))
    result.slabs = list(result.slabs) + bundled_plates_for_links(best_links)

    if core_result.decision_trace.get('_axis_core'):
        core_score = float(core_result.decision_trace['_axis_core'].get('score', 0))
    else:
        core_score = float(core_result.metrics.get('optimized_score', 0)) - float(
            core_result.metrics.get('external_independent_cost', 0))

    result.metrics['external_independent_cost'] = float(base_indep)
    result.metrics['external_optimized_cost'] = float(best_cost)
    result.metrics['external_optimization_saving'] = float(max(0.0, base_indep - best_cost))
    result.metrics['optimized_score'] = float(core_score + best_cost)
    result.metrics['small_plate_count'] = float(count_small_plates(result.slabs))

    chosen_rows = []
    for q in best_links:
        span = _vertical_span(q)
        chosen_rows.append({
            'source': q.source, 'medium': q.medium, 'target': q.target,
            'length': round(q.length, 4), 'elbows': int(q.elbows),
            'cost': round(_link_cost(q, project), 2),
            'signature': link_signature(q),
            'axis_u': round(_dominant_vertical_u(q) or q.end.u, 4),
            'vertical_span': None if not span else [round(x, 4) for x in span],
            'path': [(round(p.u, 4), round(p.v, 4)) for p in q.path],
        })

    axes = [row['axis_u'] for row in chosen_rows]
    pack_span = (max(axes) - min(axes)) if axes else 0.0
    on_magistral = pack_span <= TRANSIT_WIDTH + 1e-6 and len(set(
        q.medium for q in best_links)) >= 2

    # какие скважины сели на пучок с врезкой
    tie_u = next((_dominant_vertical_u(q) for q in best_links
                  if q.source == 'tie' or q.medium == 'oil_product'), None)
    joined = []
    if tie_u is not None:
        for q in best_links:
            if not q.source.startswith('well:'):
                continue
            u = _dominant_vertical_u(q)
            if u is not None and abs(u - tie_u) <= TRANSIT_WIDTH + 1e-6:
                joined.append(q.source)

    result.decision_trace['_external_optimization'] = {
        'mode': 'transit_magistral_v93d_one_row',
        'trials': trials,
        'improved': improved,
        'independent_cost': round(base_indep, 2),
        'optimized_cost': round(best_cost, 2),
        'saving': round(max(0.0, base_indep - best_cost), 2),
        'sharing': 'transit_magistral_0.30; shared_1.2x3_plates_once; one_plate_row',
        'magistral_span_m': round(pack_span, 4),
        'vertical_plate_rows': len({round(u, 2) for u in axes}),
        'joined_with_tie': joined,
        'chosen': chosen_rows,
        'global_optimality_proven': False,
    }

    if on_magistral or joined:
        note_m = (f' Магистраль: оси U={", ".join(f"{a:.2f}" for a in axes)} '
                  f'(разнос {pack_span:.2f} м).')
        if joined:
            note_m += f' С врезкой в пучке: {", ".join(joined)}.'
    else:
        note_m = ' Магистральный пучок с врезкой не удешевил смету — линии независимы.'

    result.notes.append(
        f'Оптимизировать: проверок {trials}, улучшений {improved}; '
        f'внешние {base_indep:,.0f} → {best_cost:,.0f} ₽ '
        f'(экономия {max(0.0, base_indep - best_cost):,.0f} ₽).'
        + note_m)

    if progress:
        progress('best_network', message='Транзитная магистраль',
                 cost=round(core_score + best_cost, 0), segments=result.segments)

    report.update({
        'trials': trials,
        'improved': improved,
        'independent_cost': round(base_indep, 2),
        'optimized_cost': round(best_cost, 2),
        'saving': round(max(0.0, base_indep - best_cost), 2),
        'core_score': round(core_score, 2),
        'total_score': round(core_score + best_cost, 2),
        'chosen': chosen_rows,
        'trial_log': trial_log,
        'unique_pipe_m': round(unique_pipe_m(best_links), 4),
        'unique_plates': unique_plate_count(best_links),
        'magistral_span_m': round(pack_span, 4),
        'joined_with_tie': joined,
        'accepted_kinds': sorted({t['kind'].split(':')[0] for t in trial_log if t['accepted']}),
    })
    return result, report


def optimize_external_combinations(project, survey, core_result, pool, *,
                                   progress=None, limit=80):
    result, _report = run_optimization_search(
        project, survey, core_result, pool, progress=progress, limit=limit)
    return result
