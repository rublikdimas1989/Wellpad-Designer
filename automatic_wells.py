"""Безопасная автоматическая расстановка внеосевых скважин.

Отдельная от ручного ввода операция. Перед выдачей координат проверяет фактическую
трассу продуктовой нефти к врезке и повторно строит весь проект с новым устьем.
Это предотвращает выдачу координат внутри защитной зоны подхода к врезке.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re

from routing import (Frame, Project, Pt, WELL_GUARD_RADIUS, build,
                     distance_segment_to_point)

# Резерв сверх обязательного радиуса, чтобы избежать округлений координат на границе.
RESERVE = 1.0


def _tie_segments(result):
    # После merge_segments назначение внешней трубы может стать «Хребет»
    # или даже «К центральному узлу от скв. N». Поэтому учитываем всю
    # продуктовую магистраль, а также участки, физически доходящие до врезки.
    # Собственные короткие отводы от скважин исключаем: их законно прокладывать
    # внутри защитной зоны подключаемой скважины.
    return [s for s in result.segments
            if s.medium == 'oil_product' and (
                not re.search(r'скв\.\s*\d+', s.purpose, re.IGNORECASE)
                or distance_segment_to_point(s.a, s.b, result.tie) < 1e-5)]


def _clearance_to_segments(point, segments):
    return min((distance_segment_to_point(seg.a, seg.b, point) for seg in segments),
               default=math.inf)


def locate_off_axis_well(project: Project, number: int) -> tuple[float, float]:
    """Предложить X/Y для скважины, находящейся вне оси.

    `project` содержит все скважины, включая искомую (временно отмеченную
    как осевую). Действующие вручную заданные координаты других скважин не меняются.
    Если безопасный вариант не найден, выдаётся ошибка вместо опасной координаты.
    """
    project.validate()
    index = next((i for i, w in enumerate(project.wells) if w.number == number), None)
    if index is None or index == 0:
        raise ValueError('Первая скважина задаёт ось НДС, она не размещается автоматически вне оси.')

    reference = build(project)
    original_tie = _tie_segments(reference)
    frame = Frame(project)
    origin = Pt(-project.head_lead + 8.0 * max(1, index), 30.0 + 2.0 * index)
    others = [w.point for w in reference.wells if w.well.number != number]

    # Сначала стараемся сохранить привычное расположение. Если близко к врезке,
    # постепенно смещаем устье вдоль НДС, а затем пробуем другую сторону.
    shifts = (0, 4, 8, 12, 16, 22, 28, 36, 48, -4, -8, -12, -20, -30, -45, 60, 80)
    strips = (origin.v, origin.v+12, origin.v+24, -origin.v, -origin.v-12, -origin.v-24)
    min_distance = WELL_GUARD_RADIUS + RESERVE
    for v in strips:
        for shift in shifts:
            q = Pt(origin.u + shift, v)
            # Проверяем 5-метровую защитную зону именно вокруг УСТЬЯ.
            if _clearance_to_segments(q, original_tie) < min_distance:
                continue
            if any(math.hypot(q.u-other.u, q.v-other.v) < 2*WELL_GUARD_RADIUS+RESERVE
                   for other in others):
                continue
            attempt = deepcopy(project)
            candidate = attempt.wells[index]
            candidate.on_axis = False
            candidate.x, candidate.y = frame.xy(q)
            try:
                planned = build(attempt)
            except ValueError:
                continue
            # После переноса меняются длина хребта, возможная сторона зеркалирования
            # и сама точка врезки. Проверяем именно ОКОНЧАТЕЛЬНЫЙ маршрут.
            if _clearance_to_segments(q, _tie_segments(planned)) < min_distance:
                continue
            return candidate.x, candidate.y
    raise ValueError(
        f'Для скважины №{number} не найдено безопасных автоматических координат. '
        'Задайте координаты вручную либо измените расположение внешних узлов.'
    )
