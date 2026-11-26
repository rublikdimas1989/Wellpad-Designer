"""2D условные обозначения пересечений без моделирования подъёма труб.

Метки обозначают выбранный для дальнейшей проработки участок трассы, а НЕ
высотное решение, спецификацию отводов или конструктивный узел.

Приоритеты:
  1) одна непрерывная нить через две и более поперечные нити — одна общая метка;
  2) для одиночных пересечений сохранять неизменным хребет и общий коридор;
  3) затем предпочитать перспективную/меньшую трубу для будущей проработки.
"""
from __future__ import annotations
from dataclasses import dataclass
from collections import defaultdict
from typing import Sequence
import csv
import math
import re

from routing import EPS, Pt, MEDIA_RU, PIPE_OD, MEDIA_COLOR
from pipe_render import Shape, pipe_width, make_network
from central_plate import PLATE_PURPOSE

# Схематические эквиваленты, НЕ цена фитингов и не инженерный высотный расчёт.
# Повторное использование фасонного элемента оправдывает не более 3,5 м
# дополнительного условного верхнего участка по сравнению с компактной меткой.
REUSED_FITTING_EQUIVALENT_M = 3.5
DEFAULT_GROUP_MAX_SPAN_M = 6.0


@dataclass(frozen=True)
class Run:
    medium: str
    phase: str
    orientation: str  # u or v
    fixed: float
    low: float
    high: float
    purposes: tuple[str, ...]

    def station(self, point: Pt) -> float:
        return point.u if self.orientation == 'u' else point.v

    def point(self, value: float) -> Pt:
        return Pt(value, self.fixed) if self.orientation == 'u' else Pt(self.fixed, value)

    def includes(self, point: Pt, margin: float = 0.) -> bool:
        cross = point.v if self.orientation == 'u' else point.u
        return (abs(cross - self.fixed) < 1e-5 and
                self.low + margin < self.station(point) < self.high - margin)

    @property
    def is_trunk(self) -> bool:
        return any(desc in ('Хребет','Перспективный хребет',
                            'Перспективное продление хребта') for desc in self.purposes)

    @property
    def protected(self) -> bool:
        return any(any(word in purpose.casefold() for word in
                       ('хребет', 'хребта', 'коридор', 'общая нитка', 'магистраль'))
                   for purpose in self.purposes)


@dataclass(frozen=True)
class Intersection:
    point: Pt
    a: int
    b: int


@dataclass(frozen=True)
class Marker:
    number: int
    run_id: int
    indices: tuple[int, ...]
    start: Pt
    end: Pt
    starts_at_tee: bool = False
    ends_at_tee: bool = False
    starts_at_elbow: bool = False
    ends_at_elbow: bool = False


@dataclass
class Analysis:
    runs: list[Run]
    crossings: list[Intersection]
    markers: list[Marker]
    unassigned: list[int]
    border_distance: float

    @property
    def grouped_count(self):
        return sum(len(mark.indices) >= 2 for mark in self.markers)

    def marker_for_crossing(self, index: int) -> Marker | None:
        return next((mark for mark in self.markers if index in mark.indices), None)

    def upper_run_id(self, index: int) -> int | None:
        mark=self.marker_for_crossing(index)
        return None if mark is None else mark.run_id

    def lower_run_id(self, index: int) -> int | None:
        mark=self.marker_for_crossing(index)
        if mark is None:
            return None
        cross=self.crossings[index]
        return cross.b if cross.a == mark.run_id else cross.a


def runs_from_segments(segments) -> list[Run]:
    parts = defaultdict(list)
    for seg in segments:
        if seg.length < EPS:
            continue
        ori = 'u' if seg.horizontal else 'v'
        fixed = seg.a.v if ori == 'u' else seg.a.u
        a, b = sorted((seg.a.u, seg.b.u) if ori == 'u' else (seg.a.v, seg.b.v))
        parts[(seg.medium, seg.phase, ori, round(fixed, 6))].append((a, b, seg.purpose))
    out = []
    for (medium, phase, orientation, fixed), spans in sorted(parts.items()):
        spans.sort()
        low, high, purpose = spans[0]
        purposes = {purpose}
        for a, b, desc in spans[1:]:
            if a <= high + 1e-6:
                high = max(high, b)
                purposes.add(desc)
            else:
                out.append(Run(medium, phase, orientation, fixed, low, high,
                               tuple(sorted(purposes))))
                low, high, purposes = a, b, {desc}
        out.append(Run(medium, phase, orientation, fixed, low, high,
                       tuple(sorted(purposes))))
    return out


def find_crossings(runs: Sequence[Run]) -> list[Intersection]:
    found = []
    seen = set()
    for i, a in enumerate(runs):
        for j in range(i + 1, len(runs)):
            b = runs[j]
            if a.medium == b.medium or a.orientation == b.orientation:
                continue
            pt = Pt(a.fixed if a.orientation == 'v' else b.fixed,
                    a.fixed if a.orientation == 'u' else b.fixed)
            if not a.includes(pt, 1e-5) or not b.includes(pt, 1e-5):
                continue
            key = (round(pt.u, 5), round(pt.v, 5),
                   tuple(sorted((a.medium, b.medium))))
            if key in seen:
                continue
            seen.add(key)
            found.append(Intersection(pt, i, j))
    return sorted(found, key=lambda c: (c.point.u, c.point.v))


def _support_stations(run: Run, segments) -> dict[float, set[str]]:
    """Определяем РЕАЛЬНЫЕ фасонные узлы в графе собственной среды и фазы.

    Объединяем соосные сегменты и режем их в точках подключений. Это важно:
    отдельные исходные сегменты могут оканчиваться в прямой нитке и не должны
    ошибочно считаться отводами, а Т-узел может быть не разрезан в исходнике.
    """
    same = [s for s in segments if s.medium == run.medium and s.phase == run.phase]
    network = make_network(same).get(run.medium, [])
    graph = defaultdict(set)
    for a, b, phase in network:
        graph[a].add(b)
        graph[b].add(a)
    supports: dict[float, set[str]] = defaultdict(set)
    for (u, v), neighbors in graph.items():
        if run.orientation == 'u':
            if abs(v-run.fixed) > 1e-5 or not run.low-1e-5 <= u <= run.high+1e-5:
                continue
            station = u
        else:
            if abs(u-run.fixed) > 1e-5 or not run.low-1e-5 <= v <= run.high+1e-5:
                continue
            station = v
        if len(neighbors) >= 3:
            supports[round(station,6)].add('tee')
        elif len(neighbors) == 2:
            a,b = neighbors
            da=(a[0]-u, a[1]-v)
            db=(b[0]-u, b[1]-v)
            # Перпендикулярные векторы — существующий отвод 90°.
            # Коллинеарное соединение НЕ фасонный элемент.
            if abs(da[0]*db[0]+da[1]*db[1]) < 1e-8:
                supports[round(station,6)].add('elbow')
    return dict(supports)


def _support_bonus(kinds: set[str]) -> int:
    # Это оценка экономии ОДНОГО нового элемента на одной границе.
    return 1 if kinds.intersection({'tee', 'elbow'}) else 0


def _span(run: Run, stations: list[float], border: float,
          supports: dict[float, set[str]] | None = None,
          reuse_reach: float = 12.0, all_stations: list[float] | None = None):
    """Выбрать допустимые границы с приоритетом существующих отводов/тройников.

    Ошибка v30: узел использовался только если попадал в +/-border=0,75 м.
    Теперь ищем узел в пределах reuse_reach от крайнего пересечения.
    Для существующего узла проверяем увеличение условной длины: экономия
    одного фасонного элемента не должна оправдывать десятки метров сверху.
    Узлы строго внутри условного поднятого участка не допускаем.
    Реальное высотное решение и радиусы в 2D-модели не рассчитываются.
    """
    supports = supports or {}
    first, last = min(stations), max(stations)
    minimum = .05
    if first <= run.low+minimum or last >= run.high-minimum:
        return None
    # Разрыв нельзя провести через реальный фасонный узел: его нужно
    # использовать как границу или выбрать другую трассу.
    if any(first-minimum <= t <= last+minimum for t in supports):
        return None
    before = sorted(t for t in supports if run.low-1e-5 <= t <= first-minimum)
    after = sorted(t for t in supports if last+minimum <= t <= run.high+1e-5)

    def choices(st: float, is_start: bool):
        standard = st-border if is_start else st+border
        bound = run.low if is_start else run.high
        out = []
        if (standard > bound+minimum if is_start else standard < bound-minimum):
            out.append(standard)
        candidates = (before if is_start else after)
        # Не пропускаем через промежуточный узел: лишь ближайший может быть
        # границей, и только если он не дальше заданного предела.
        near = (max(candidates) if is_start else min(candidates)) if candidates else None
        if near is not None and abs(near-st) <= reuse_reach + 1e-8:
            out.append(near)
        return sorted(set(round(x,6) for x in out))

    opts_l, opts_r = choices(first,True), choices(last,False)
    candidates = []
    for lo in opts_l:
        for hi in opts_r:
            if not lo + minimum < first or not last < hi - minimum:
                continue
            if any(lo+1e-5 < t < hi-1e-5 for t in supports):
                continue
            if all_stations is not None and any(lo+1e-5 < t < hi-1e-5
                                                and all(abs(t-st)>1e-5 for st in stations)
                                                for t in all_stations):
                # Дополнительное незаявленное пересечение попало под подъём.
                continue
            benefit = (_support_bonus(supports.get(round(lo,6),set())) +
                       _support_bonus(supports.get(round(hi,6),set())))
            # Суммарный условный критерий, а не безусловный приоритет
            # любого тройника/отвода на расстоянии до 12 м.
            # 2 границы потенциально требуют по одному новому элементу;
            # повторно используемый узел даёт небольшой метрический бонус.
            # При равной цене — минимальная длина и затем более короткие подходы.
            length=hi-lo
            cost=length + (2-benefit)*REUSED_FITTING_EQUIVALENT_M
            candidates.append((round(cost,7),round(length,7),lo,hi))
    if not candidates:
        return None
    _,_,lo,hi = min(candidates)
    return lo,hi


def _protected_plate_at(run: Run, point: Pt, segments) -> bool:
    """Магистраль внутри центральной плиты НИКОГДА не считается верхней."""
    for seg in segments:
        if seg.medium != run.medium or seg.purpose != PLATE_PURPOSE:
            continue
        if run.orientation != 'u' or not seg.horizontal:
            continue
        if (abs(seg.a.v - point.v) < 1e-5 and
                min(seg.a.u,seg.b.u)-1e-6 <= point.u <= max(seg.a.u,seg.b.u)+1e-6):
            return True
    return False


def _is_axial_offtake(run: Run, axial_well_ids: set[int]) -> bool:
    """Провести осевую отпайку выше хребта независимо от группировки."""
    if run.is_trunk:
        return False
    for description in run.purposes:
        match = re.search(r'отвод от скв\.\s*(\d+)', description, re.I)
        if match and int(match.group(1)) in axial_well_ids:
            return True
    return False


def _mandatory_axial_top(cross: Intersection, runs: list[Run],
                         axial_well_ids: set[int]) -> int | None:
    a, b = cross.a, cross.b
    if runs[a].is_trunk and _is_axial_offtake(runs[b], axial_well_ids):
        return b
    if runs[b].is_trunk and _is_axial_offtake(runs[a], axial_well_ids):
        return a
    return None


def analyze(segments, border_distance: float = .75,
            group_max_span: float = DEFAULT_GROUP_MAX_SPAN_M,
            axial_well_ids=()) -> Analysis:
    if not math.isfinite(border_distance) or not .1 <= border_distance <= 30.:
        raise ValueError('Отступ границы участка: допустимо от 0,1 до 30 м')
    if not math.isfinite(group_max_span) or not .5 <= group_max_span <= 30.:
        raise ValueError('Предельный размах группы должен быть от 0,5 до 30 м')
    runs = runs_from_segments(segments)
    crossings = find_crossings(runs)
    axial_ids = {int(x) for x in axial_well_ids}
    mandatory_top = {i: _mandatory_axial_top(cross, runs, axial_ids)
                     for i, cross in enumerate(crossings)}
    supports = [_support_stations(run, segments) for run in runs]
    by_run = defaultdict(list)
    for ix, c in enumerate(crossings):
        by_run[c.a].append(ix)
        by_run[c.b].append(ix)

    # Все возможные непрерывные группы с хотя бы ДВУМЯ разными поперечными
    # нитками. Приоритет групп над одиночными метками — строгий, не весовой.
    candidates = []
    for run_id, indices in by_run.items():
        run = runs[run_id]
        indices.sort(key=lambda ix: run.station(crossings[ix].point))
        for i, first in enumerate(indices):
            others = set()
            stations = []
            for last in indices[i:]:
                c = crossings[last]
                other_id = c.b if c.a == run_id else c.a
                st = run.station(c.point)
                if stations and st - stations[0] > group_max_span:
                    break
                others.add(other_id)
                stations.append(st)
                if len(stations) < 2 or len(others) < 2 or len(set(stations)) < 2:
                    continue
                bounds = _span(run, stations, border_distance, supports[run_id],
                               all_stations=[run.station(crossings[ix].point) for ix in indices])
                if bounds is None:
                    continue
                selected = tuple(indices[i:i + len(stations)])
                if any(mandatory_top[ix] is not None and mandatory_top[ix] != run_id
                       for ix in selected):
                    continue  # осевой отпаек ВСЕГДА выше хребта, даже при группировке
                if any(_protected_plate_at(run,crossings[ix].point,segments)
                       for ix in selected):
                    continue  # поднять магистральную трубу плиты запрещено
                # group length dominates: 3 through 1 beats 2 through 1;
                # if tied, prefer shorter marker and non-protected route.
                left_kinds = supports[run_id].get(round(bounds[0], 6), set())
                right_kinds = supports[run_id].get(round(bounds[1], 6), set())
                added_fittings = 4 - _support_bonus(left_kinds) - _support_bonus(right_kinds)
                # При равном количестве перекрытых ниток сравниваем
                # длину + стоимость дополнительных элементов (условные м).
                # Иначе один отвод мог «законно» удлинять верхний участок на 12 м.
                length=bounds[1]-bounds[0]
                relative_cost=length + added_fittings*REUSED_FITTING_EQUIVALENT_M
                candidates.append((int(run.is_trunk), -len(selected), int(run.protected),
                                   round(relative_cost,7), length,
                                   run_id, selected, bounds))
    candidates.sort()
    reserved = set()
    picked = []
    for _, _, _, _, _, run_id, indices, bounds in candidates:
        if reserved.intersection(indices):
            continue
        reserved.update(indices)
        picked.append((run_id, indices, bounds))

    # Одиночное пересечение: хребтовую/коридорную нитку приоритетно
    # оставляем неизменной. Метка ставится на другую трубу.
    unassigned = []
    for ix, cross in enumerate(crossings):
        if ix in reserved:
            continue
        options = []
        for rid in (cross.a, cross.b):
            if mandatory_top[ix] is not None and rid != mandatory_top[ix]:
                continue
            run = runs[rid]
            if _protected_plate_at(run,cross.point,segments):
                continue  # только подводимая нитка может идти поверх плиты
            st = run.station(cross.point)
            bounds = _span(run, [st], border_distance, supports[rid],
                           all_stations=[run.station(crossings[ci].point) for ci in by_run[rid]])
            if bounds is None:
                continue
            left_kinds = supports[rid].get(round(bounds[0], 6), set())
            right_kinds = supports[rid].get(round(bounds[1], 6), set())
            added_fittings = 4 - _support_bonus(left_kinds) - _support_bonus(right_kinds)
            length=bounds[1]-bounds[0]
            relative_cost=length+added_fittings*REUSED_FITTING_EQUIVALENT_M
            score = (int(run.is_trunk), int(run.protected), round(relative_cost,7),
                     0 if run.phase == 'future' else 1,
                     PIPE_OD.get(run.medium, 100.), length, rid)
            options.append((score, rid, bounds))
        if options:
            _, rid, bounds = min(options)
            picked.append((rid, (ix,), bounds))
        else:
            unassigned.append(ix)

    picked.sort(key=lambda item: (min(crossings[ix].point.u for ix in item[1]),
                                  min(crossings[ix].point.v for ix in item[1]), item[0]))
    markers = [Marker(i + 1, rid, ids, runs[rid].point(bounds[0]),
                      runs[rid].point(bounds[1]),
                      'tee' in supports[rid].get(round(bounds[0], 6), set()),
                      'tee' in supports[rid].get(round(bounds[1], 6), set()),
                      'elbow' in supports[rid].get(round(bounds[0], 6), set()),
                      'elbow' in supports[rid].get(round(bounds[1], 6), set()))
               for i, (rid, ids, bounds) in enumerate(picked)]
    return Analysis(runs, crossings, markers, unassigned, border_distance)


def visual_cover_primitives(analysis: Analysis, xy, pixels_per_meter: float, detailed: bool = True, background: str = "#ffffff", central_plate=None, support_slabs=()):
    """Чисто графическое перекрытие: верхняя труба визуально закрывает нижнюю.

    Никаких отметок Z и 3D-геометрии здесь нет: белая маска только разрывает
    нижний контур на плане, после чего выбранная труба перерисовывается поверх.
    Это делается для читаемости схемы, а не как конструкторское решение.
    """
    masks: list[Shape] = []
    top_lines: list[Shape] = []
    for marker in analysis.markers:
        run = analysis.runs[marker.run_id]
        a, b = xy(marker.start), xy(marker.end)
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = max(1e-8, math.hypot(dx, dy))
        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux
        top_width = pipe_width(PIPE_OD[run.medium], pixels_per_meter) if detailed else (3.2 if run.phase=='active' else 2.6)
        half = top_width / 2.0
        # Маска только в самой точке пересечения: не перекрываем Т-узел
        # на границе участка и соседние подключения той же среды.
        for ix in marker.indices:
            crossing = analysis.crossings[ix]
            other_id = crossing.b if crossing.a == marker.run_id else crossing.a
            lower = analysis.runs[other_id]
            low_width = pipe_width(PIPE_OD[lower.medium], pixels_per_meter) if detailed else (3.2 if lower.phase=='active' else 2.6)
            # Запас для скрытия цветного контура и пунктирной оси нижней трубы.
            longitudinal = low_width / 2 + 2.3
            transverse = half + 2.5
            x, y = xy(crossing.point)
            # Не заходим белой маской на Т-узел, которым ограничен
            # переход (если расстояние до узла меньше толщины в пикселях).
            from_start = (x-a[0])*ux + (y-a[1])*uy
            to_end = (b[0]-x)*ux + (b[1]-y)*uy
            backward = min(longitudinal, max(0.6, from_start-0.7))
            forward = min(longitudinal, max(0.6, to_end-0.7))
            corners = tuple((x + ux*along + nx*across,
                             y + uy*along + ny*across)
                            for along, across in ((-backward,-transverse),
                                                  (forward,-transverse),
                                                  (forward,transverse),
                                                  (-backward,transverse)))
            # Даже если пересечение находится над плитой, плита конструктивно ниже.
            # Поэтому маска не должна возвращать серый цвет плиты в просвет трубы.
            paint = background
            masks.append(Shape('polygon', corners, paint, 0.0, paint))
        # Перерисовываем только короткие участки ПОПЕРЁК каждого пересечения,
        # а НЕ всю длину метки (иначе линия проходила сквозь существующий
        # отвод/тройник и визуально скрывала фасонную деталь).
        for ix in marker.indices:
            crossing = analysis.crossings[ix]
            other_id = crossing.b if crossing.a == marker.run_id else crossing.a
            lower = analysis.runs[other_id]
            lw = pipe_width(PIPE_OD[lower.medium], pixels_per_meter) if detailed else (3.2 if lower.phase=='active' else 2.6)
            pt = xy(crossing.point)
            margin = lw / 2 + 3.2
            from_start = (pt[0]-a[0])*ux + (pt[1]-a[1])*uy
            to_end = (b[0]-pt[0])*ux + (b[1]-pt[1])*uy
            back = min(margin,max(0.5,from_start-1.0))
            fwd = min(margin,max(0.5,to_end-1.0))
            p = (pt[0]-ux*back,pt[1]-uy*back)
            q = (pt[0]+ux*fwd,pt[1]+uy*fwd)
            if detailed:
                dash = '7 5' if run.phase == 'future' else None
                color = MEDIA_COLOR[run.medium]
                for sign in (-1,1):
                    edge = ((p[0]+nx*half*sign,p[1]+ny*half*sign),
                            (q[0]+nx*half*sign,q[1]+ny*half*sign))
                    top_lines.append(Shape('line',edge,color,1.8,None,dash))
                top_lines.append(Shape('line',(p,q),'#171717',1.1,None,'8 3 1 3'))
            else:
                dash = '10 6' if run.phase == 'future' else None
                top_lines.append(Shape('line',(p,q),MEDIA_COLOR[run.medium],top_width,None,dash))
    # Сначала скрываются все нижние трубы, затем восстанавливаются верхние.
    return masks + top_lines


def overlay_primitives(analysis: Analysis, xy, scale=1.):
    """Только границы/подписи в 2D. Подписи разводятся, чтобы не слипались."""
    shapes = []
    labels = []
    for m in analysis.markers:
        a, b = xy(m.start), xy(m.end)
        dx, dy = b[0]-a[0], b[1]-a[1]
        length = max(1e-8, math.hypot(dx, dy))
        nx, ny = -dy / length, dx / length
        for p in (a, b):
            shapes.append(('line', (p[0]-nx*5, p[1]-ny*5),
                           (p[0]+nx*5, p[1]+ny*5), '#282828', 1.5))
        mid = ((a[0]+b[0])/2, (a[1]+b[1])/2)
        description = f'У{m.number}' + (f' [{len(m.indices)}]' if len(m.indices) > 1 else '')
        labels.append((len(m.indices)>1, m.number, mid, nx, ny, description))

    # На масштабах, где соседние нитки почти сливаются в несколько пикселей,
    # размещаем по возможности непересекающиеся подписи. Сам штрих границы
    # остаётся всегда; все номера дополнительно доступны в CSV.
    occupied = []
    for _, _, mid, nx, ny, description in sorted(labels, key=lambda x: (-int(x[0]), x[1])):
        width = max(19., len(description) * 6.4)
        for dist, along in ((12.,0.),(-12.,0.),(25.,0.),(-25.,0.),
                            (12.,20.),(-12.,20.),(12.,-20.),(-12.,-20.)):
            px = mid[0] + nx * dist - ny * along
            py = mid[1] + ny * dist + nx * along
            box = (px-1,py-12,px+width+2,py+3)
            if any(not (box[2]+3 < old[0] or old[2]+3 < box[0] or
                        box[3]+2 < old[1] or old[3]+2 < box[1]) for old in occupied):
                continue
            occupied.append(box)
            shapes.append(('text', (px,py), description, '#333333', 10.))
            break
    for ix in analysis.unassigned:
        x, y = xy(analysis.crossings[ix].point)
        shapes.append(('text', (x+5, y-6), '×', '#b91c1c', 15.))
    return shapes

def export_crossings_csv(result, path):
    model = result.crossing_markup
    if model is None:
        raise ValueError('Нет данных анализа пересечений')
    assigned = {ix: m for m in model.markers for ix in m.indices}
    with open(path, 'w', newline='', encoding='utf-8-sig') as stream:
        w = csv.writer(stream, delimiter=';')
        w.writerow(['№ пересечения', 'X, м', 'Y, м', 'Среда 1', 'Среда 2',
                    'Условный участок', 'Отмечаемая труба', 'Верхняя труба', 'Нижняя труба', 'Тип',
                    'X начала метки', 'Y начала метки', 'X конца метки', 'Y конца метки',
                    'Начало на существующем тройнике', 'Конец на существующем тройнике',
                    'Начало на существующем отводе', 'Конец на существующем отводе',
                    'Длина условного участка 2D, м', 'Примечание'])
        for ix, c in enumerate(model.crossings):
            mark = assigned.get(ix)
            x, y = result.frame.xy(c.point)
            names = (MEDIA_RU[model.runs[c.a].medium], MEDIA_RU[model.runs[c.b].medium])
            upper = model.upper_run_id(ix)
            lower = model.lower_run_id(ix)
            upper_name = MEDIA_RU[model.runs[upper].medium] if upper is not None else ''
            lower_name = MEDIA_RU[model.runs[lower].medium] if lower is not None else ''
            if mark:
                a, b = result.frame.xy(mark.start), result.frame.xy(mark.end)
                label = f'У{mark.number}'
                medium = MEDIA_RU[model.runs[mark.run_id].medium]
                kind = 'Общий участок через несколько ниток' if len(mark.indices) > 1 else 'Одиночное пересечение'
            else:
                a, b = ('', ''), ('', '')
                label, medium, kind = '', '', 'Не удалось поставить границы'
            w.writerow([ix + 1, round(x, 3), round(y, 3), *names, label, medium, upper_name, lower_name, kind,
                        *(round(value, 3) if isinstance(value, (float, int)) else value for value in (*a, *b)),
                        'Да' if mark and mark.starts_at_tee else 'Нет',
                        'Да' if mark and mark.ends_at_tee else 'Нет',
                        'Да' if mark and mark.starts_at_elbow else 'Нет',
                        'Да' if mark and mark.ends_at_elbow else 'Нет',
                        round(math.hypot(mark.end.u-mark.start.u, mark.end.v-mark.start.v),3) if mark else '',
                        'Только обозначение в плане; высотное решение не рассчитывается'])
