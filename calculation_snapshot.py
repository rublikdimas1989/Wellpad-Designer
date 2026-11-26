"""Снимки этапа «Рассчитать» для последующего разбора.

Папка «Снимки_расчёта» рядом с приложением (как «Проекты»).
Сразу после независимого расчёта сохраняются настройки, скважины и
топ независимых путей каждого элемента — без пучков и тройников.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json
import os
import uuid

from catalog_solver import _link_cost, link_signature, path_signature


def snapshot_dir(app_root: Path | None = None) -> Path:
    root = Path(app_root) if app_root else Path(__file__).resolve().parent
    path = root / 'Снимки_расчёта'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path_row(q, project):
    return {
        'signature': link_signature(q),
        'source': q.source,
        'medium': q.medium,
        'target': q.target,
        'start_side': q.start_side,
        'end_side': getattr(q, 'end_side', ''),
        'target_kind': q.target_kind,
        'length_m': round(q.length, 4),
        'elbows': int(q.elbows),
        'independent_cost': round(_link_cost(q, project), 2),
        'path': [(round(p.u, 4), round(p.v, 4)) for p in q.path],
        'path_signature': path_signature(q.path),
        'status': q.status,
        'reason': q.reason,
    }


def build_snapshot(project, catalog, result=None, *, search=None, note=''):
    """Serialize settings, wells and up to 100 cheapest independent paths/slot."""
    from collections import defaultdict
    pools = defaultdict(list)
    for key, links in (catalog or {}).items():
        for q in links:
            if not q.path or q.status == 'недопустим':
                continue
            pools[(q.source, q.medium)].append(q)
    path_pool = {}
    for (source, medium), rows in pools.items():
        ranked = sorted(rows, key=lambda q: (_link_cost(q, project), q.elbows, q.length, q.target))
        uniq = []
        seen = set()
        for q in ranked:
            sig = link_signature(q)
            if sig in seen:
                continue
            seen.add(sig)
            uniq.append(_path_row(q, project))
            if len(uniq) >= 100:
                break
        path_pool[f'{source}|{medium}'] = uniq
    selected = []
    if result is not None:
        selected = list(result.decision_trace.get('_selected_links') or [])
    return {
        'version': 90,
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'note': note or 'Снимок после «Рассчитать»: осевое ядро (внешние трассы отключены)',
        'project': asdict(project),
        'wells': [asdict(w) for w in project.wells],
        'settings': {
            'pipe_cost_per_m': project.pipe_cost_per_m,
            'elbow_cost': project.elbow_cost,
            'tee_cost': project.tee_cost,
            'plate_cost': project.plate_cost,
            'lane_gap': project.lane_gap,
            'shared_corridors': project.shared_corridors,
            'offset': project.offset,
            'azimuth': project.azimuth,
            'fire_medium': project.fire_medium,
            'auto_rotate': project.auto_rotate,
            'tie_x': project.tie_x,
            'tie_y': project.tie_y,
            'fire_x': project.fire_x,
            'fire_y': project.fire_y,
            'well_guard_radius': getattr(project, 'well_guard_radius', 5.0),
        },
        'search': search or {},
        'path_pool_top100': path_pool,
        'path_pool_counts': {k: len(v) for k, v in path_pool.items()},
        'selected_independent': selected,
        'independent_score': None if result is None else float(result.metrics.get('optimized_score', 0)),
    }


def save_calculation_snapshot(project, catalog, result=None, *, search=None,
                              app_root=None, name_hint='') -> Path:
    folder = snapshot_dir(app_root)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in (name_hint or 'kust'))[:40]
    path = folder / f'calc_{stamp}_{safe}_{uuid.uuid4().hex[:6]}.json'
    data = build_snapshot(project, catalog, result, search=search)
    temp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return path
