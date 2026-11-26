"""Центральный узел — площадной модуль, а не точка.

На центральной плите линии идут без подъёмов, параллельно оси НДС, на всю
длину. Хребет присоединяется к её внутреннему торцу, внешние линии — к
свободному торцу или боковым портам. Наложения разных сред лишь 2D-эскиз.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable

PLATE_LENGTH = 6.0
PLATE_WIDTH = 1.75
PLATE_PURPOSE = 'Центральная плита: магистральная труба'
# Для внешних транзитных линий торцевой ввод этих сред запрещён.
# Хребет и независимый пожарный узел имеют отдельные порты по назначению.
TRANSIT_SIDE_ONLY=frozenset(('fresh','pressure','oil_product'))

@dataclass(frozen=True)
class PlateGeometry:
    u_min: float
    u_max: float
    v_min: float
    v_max: float
    trunk_face_u: float
    outer_face_u: float

    @property
    def center_u(self):
        return (self.u_min+self.u_max)/2

    @property
    def center_v(self):
        return (self.v_min+self.v_max)/2

    def flipped(self, L: float) -> 'PlateGeometry':
        """Преобразовать канонический левый модуль в правый относительно L."""
        return PlateGeometry(L-self.u_max, L-self.u_min,
                             -self.v_max, -self.v_min,
                             L-self.trunk_face_u,L-self.outer_face_u)

    def contains(self, point, margin=1e-6):
        return (self.u_min-margin <= point.u <= self.u_max+margin
                and self.v_min-margin <= point.v <= self.v_max+margin)

    def port(self, medium, lanes, face='outer', fraction=.5):
        """Явный порт каждой магистрали на торце или в боковой части плиты."""
        from routing import Pt
        if face == 'outer': u=self.outer_face_u
        elif face == 'trunk': u=self.trunk_face_u
        elif face == 'side': u=self.outer_face_u + fraction*(self.trunk_face_u-self.outer_face_u)
        else: raise ValueError('Порт центральной плиты: outer, trunk или side')
        return Pt(u,lanes[medium])


def make_plate(head_u, lanes, pipe_od, length=PLATE_LENGTH, width=PLATE_WIDTH):
    """Хребет на правом торце; пять параллельных линий внутри 1,75 м.

    Существующий lane_gap задаёт ЧИСТЫЙ зазор, фактическое расстояние между
    осями ≈0,35—0,39 м для диаметров из предварительного каталога.
    """
    lower=min(lanes[m]-pipe_od[m]/2 for m in lanes)
    upper=max(lanes[m]+pipe_od[m]/2 for m in lanes)
    span=upper-lower
    if span > width+1e-6:
        raise ValueError(f'Пять труб с заданными зазорами не помещаются на центральной плите '
                         f'{width:.2f} м: требуется {span:.3f} м. Уменьшите зазор хребта.')
    middle=(lower+upper)/2
    return PlateGeometry(head_u-length,head_u,middle-width/2,middle+width/2,
                         head_u,head_u-length)


def central_pipe_segments(plate, lanes, media, installed_phase):
    from routing import Segment, Pt
    media=tuple(media)
    return [Segment(Pt(plate.outer_face_u,lanes[m]),
                    Pt(plate.trunk_face_u,lanes[m]),
                    m,installed_phase.get(m,'active'),PLATE_PURPOSE)
            for m in media]


def external_plate_ports(plate, lanes, medium, purpose='transit'):
    """Допустимые порты согласно назначению линии, не только её среде.

    transit: СППВ/ППД/нефть только сбоку; для остальных сред торец возможен.
    fire: только наружный торец; trunk: только внутренний торец.
    """
    if purpose == 'fire':
        return [plate.port(medium,lanes,'outer')]
    if purpose == 'trunk':
        return [plate.port(medium,lanes,'trunk')]
    if purpose != 'transit':
        raise ValueError(f'Неизвестный тип подключения плиты: {purpose}')
    ports=[plate.port(medium,lanes,'side',f) for f in (.25,.50,.75)]
    if medium not in TRANSIT_SIDE_ONLY:
        ports.insert(0,plate.port(medium,lanes,'outer'))
    return ports


def plate_draw_data(plate):
    return ((plate.u_min,plate.v_min),(plate.u_max,plate.v_min),
            (plate.u_max,plate.v_max),(plate.u_min,plate.v_max))


def capped_ports(plate, segments, lanes, media):
    """Возвращает торцы, на которых нет продолжения этой трубы за плиту."""
    from routing import Pt, EPS
    caps=[]
    for m in media:
        for face,loc,sgn in (('outer',plate.outer_face_u,
                              -1 if plate.outer_face_u<plate.trunk_face_u else 1),
                             ('trunk',plate.trunk_face_u,
                              1 if plate.outer_face_u<plate.trunk_face_u else -1)):
            is_connected=False
            for seg in segments:
                if seg.medium!=m or seg.purpose==PLATE_PURPOSE:continue
                if abs(seg.a.v-lanes[m])>1e-6 or abs(seg.b.v-lanes[m])>1e-6:continue
                if min(seg.a.u,seg.b.u)<=loc+EPS and max(seg.a.u,seg.b.u)>=loc-EPS:
                    # segment must actually leave the face; just touching is not enough
                    if sgn>0 and max(seg.a.u,seg.b.u)>loc+EPS:is_connected=True
                    if sgn<0 and min(seg.a.u,seg.b.u)<loc-EPS:is_connected=True
            if not is_connected:caps.append((m,Pt(loc,lanes[m]),face))
    return caps
