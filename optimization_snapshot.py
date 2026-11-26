"""Снимки этапа «Оптимизировать» для последующего разбора.

Папка «Снимки_оптимизации» рядом с приложением.
После оптимизации сохраняются настройки, скважины, выбранные пути,
пул, журнал проверок и итоговая смета.
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
    path = root / 'Снимки_оптимизации'
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


def build_optimization_snapshot(project, pool, result, report, *, note=''):
    path_pool = {}
    for key, variants in (pool or {}).items():
        path_pool[key] = [_path_row(q, project) for q in variants]
    return {
        'version': 92,
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'note': note or 'Снимок после «Оптимизировать»: комбинации внешних путей',
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
            'tie_x': project.tie_x,
            'tie_y': project.tie_y,
            'fire_x': project.fire_x,
            'fire_y': project.fire_y,
        },
        'report': report or {},
        'path_pool': path_pool,
        'path_pool_counts': {k: len(v) for k, v in path_pool.items()},
        'chosen_optimized': (result.decision_trace.get('_external_optimization') or {}).get('chosen', []),
        'metrics': {
            'optimized_score': float(result.metrics.get('optimized_score', 0)),
            'external_independent_cost': float(result.metrics.get('external_independent_cost', 0)),
            'external_optimized_cost': float(result.metrics.get('external_optimized_cost', 0)),
            'external_optimization_saving': float(result.metrics.get('external_optimization_saving', 0)),
            'small_plate_count': float(result.metrics.get('small_plate_count', 0)),
            'pipe_m': float(result.metrics.get('pipe_m', 0)),
        },
        'notes': list(result.notes[-12:]),
    }


def save_optimization_snapshot(project, pool, result, report, *,
                               app_root=None, name_hint='') -> Path:
    folder = snapshot_dir(app_root)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in (name_hint or 'kust'))[:40]
    path = folder / f'opt_{stamp}_{safe}_{uuid.uuid4().hex[:6]}.json'
    data = build_optimization_snapshot(project, pool, result, report)
    temp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return path
