"""Эскизный 2D-проектировщик: ЕДИНЫЙ ХРЕБЕТ, центр в его начале.

Пять фиксированных позиций сверху вниз (на плане):
СППВ, ППД, замер нефти, газ, продуктовая нефть.
Центральная плита и плиты осевых отпайков отображаются в масштабе; транзитные МИС отложены.
Замерный нефтепровод не имеет индивидуальных отводов от скважин.
Вся геометрия — строго вдоль/поперёк направления оси НДС.
Это предварительная трассировка, НЕ проектная документация и НЕ проверка нормативов.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path
import math, json, csv, heapq, re
from itertools import permutations, product, product as iterproduct
from central_plate import (make_plate, central_pipe_segments, external_plate_ports, PLATE_PURPOSE, PLATE_LENGTH, TRANSIT_SIDE_ONLY)

EPS=1e-7
WELL_GUARD_RADIUS=5.0  # Запретная зона для ЧУЖИХ коммуникаций (м)
GUARD_BUFFER=0.15       # Геометрический резерв, чтобы не идти строго по границе
MEDIA=('fresh','pressure','oil_measure','gas','oil_product')
MEDIA_RU={'fresh':'Пресная вода (СППВ)','pressure':'Пластовая вода ППД',
          'oil_measure':'Замерной нефтепровод','gas':'Газ',
          'oil_product':'Продуктовый нефтепровод'}
MEDIA_COLOR={'fresh':'#136bdb','pressure':'#647923','oil_measure':'#ff00ff',
             'gas':'#d43a45','oil_product':'#ff00ff'}
# Сторонние подключения этих сред к плите только с длинной грани.
# Хребет и независимый пожарный вывод с торцов — исключения.
# СППВ, ППД, газ и продуктовая нефть вне хребта заходят в плиту только сбоку.
SIDE_ONLY_PLATE_MEDIA=frozenset((*TRANSIT_SIDE_ONLY, "gas"))
WELL_KINDS=('producer','injector','ppd_source','fresh_source')
KIND_RU={'producer':'Добывающая','injector':'Нагнетательная',
         'ppd_source':'Водозаборная ППД','fresh_source':'Водозаборная СППВ'}
PIPE_OD={'fresh':.057,'pressure':.159,'oil_measure':.114,'gas':.114,'oil_product':.159}  # Газ условно DN114, требует уточнения
STATUS_RU={'active':'Обустраиваемая','inactive':'Не обустраиваемая','future':'Перспектива'}

@dataclass(frozen=True)
class Pt:
    u:float
    v:float

@dataclass
class Well:
    number:int
    kind:str='producer'
    status:str='active'
    on_axis:bool=True
    x:float|None=None
    y:float|None=None
    gas:bool=False
    orientation:str="parallel"  # направление ДЛИННОЙ стороны прямоугольника относительно оси НДС
    rotation_locked:bool=False
    planned_media:list[str]=field(default_factory=list)
    exit_direction:str='auto'  # auto / u+ / u- / v+ / v-: фактическая сторона выхода
    def validate(self):
        if self.exit_direction not in ('auto','u+','u-','v+','v-'):
            raise ValueError(f'Скважина {self.number}: допустимые стороны выхода: auto, u+, u-, v+, v-')
        if self.orientation not in ("parallel", "perpendicular"):
            raise ValueError(f"Скважина {self.number}: неверная ориентация")
        if self.kind not in WELL_KINDS or self.status not in STATUS_RU:
            raise ValueError(f'Скважина {self.number}: неверный тип или статус')
        wrong=[m for m in self.planned_media if m not in MEDIA]
        if wrong:
            raise ValueError(f'Скважина {self.number}: недопустимые перспективные линии: {wrong}')
        if not self.on_axis and (self.x is None or self.y is None or not all(math.isfinite(t) for t in (self.x,self.y))):
            raise ValueError(f'Скважина {self.number}: нужны координаты X и Y вне оси')

@dataclass
class Project:
    first_x:float=100.0
    first_y:float=100.0
    tie_x:float=200.0
    tie_y:float=90.0
    fire_x:float=112.75
    fire_y:float=53.0
    azimuth:float=90.0
    coordinate_mode:str='x_north'
    head_lead:float=12.0       # Отступ до ГЕОМЕТРИЧЕСКОГО ЦЕНТРА центральной плиты от первой/последней осевой скважины, м
    offset:float=12.0          # Ближайшая труба от оси НДС: новый стандарт по умолчанию
    lane_gap:float=0.25         # Для пяти хребтовых ниток: 0.30 м не помещается на плите 1.75 м; внешние транзиты >=0.30 м
    slab_step:float=6.75       # Шаг плит хребта (начало-начало), м
    slab_axis_size:float=2.4   # Длина плиты хребта вдоль НДС, м
    branch_plate_offset:float=1.0  # Отступ начала плиты отпайки от скважины, м
    center_x:float|None=None    # При вводе координат центральная точка фиксируется в мировой СК
    center_y:float|None=None
    corridor_weight:float=2.0  # Архивный коэффициент v38, не включать в новый J.
    pipe_cost_per_m:float=2000.0     # руб./м трубы
    elbow_cost:float=4500.0         # руб./отвод 90°
    plate_cost:float=12000.0        # руб./плита 1,2 × 3 м
    tee_cost:float=6000.0           # По умолчанию 6000 ₽ за физический тройник; редактируемая
    crossing_cost:float=20.0        # Архивный коэффициент; в простом 2D-режиме v74 не используется
    corridor_saving_per_m:float=0.0 # В v74 бонус пучков отключён (legacy JSON-поле).
    bend_penalty:float=3.0     # Эквивалент метров на один поворот
    crossing_penalty:float=20.0  # Эквивалент метров на пересечение
    trunk_side:str="auto"  # auto, positive, negative
    center_position:str="auto"  # auto, left_upper, left_lower, right_upper, right_lower
    auto_rotate:bool=True
    fire_medium:str='pressure'
    auto_overpasses:bool=True  # Обозначать участки пересечений; имя ключа сохранено для старых JSON
    vertical_clearance:float=.25  # Условный просвет поверхностей по вертикали, м
    bridge_approach:float=.5  # Отступ границ условной метки от пересечения, м
    shared_corridors:bool=True  # Сравнивать раздельную прокладку и допустимые пучки по полной J.
    tie_confirmed:bool=True     # старые проекты считаются подтверждёнными
    fire_confirmed:bool=True
    wells:list[Well]=field(default_factory=lambda:[Well(i+1) for i in range(8)])

    def validate(self):
        for k in ('first_x','first_y','tie_x','tie_y','fire_x','fire_y','azimuth','head_lead',
                  'offset','lane_gap','slab_step','slab_axis_size','branch_plate_offset','corridor_weight','bend_penalty','crossing_penalty','vertical_clearance','bridge_approach',
                  'pipe_cost_per_m','elbow_cost','plate_cost','tee_cost','crossing_cost','corridor_saving_per_m'):
            v=getattr(self,k)
            if not isinstance(v,(int,float)) or not math.isfinite(v):
                raise ValueError(f'Введите корректное число: {k}')
        if not 0<=self.azimuth<=360:
            raise ValueError('Азимут должен быть в пределах 0–360°')
        if self.coordinate_mode not in ('x_north','x_east'):
            raise ValueError('Неизвестная система координат')
        if self.trunk_side not in ('auto','positive','negative'):
            raise ValueError('Сторона хребта: авто, плюс или минус')
        if self.center_position not in ('auto','left_upper','left_lower','right_upper','right_lower','left_auto'):
            raise ValueError('Положение центрального узла: выберите авто или один из четырёх углов')
        if self.fire_medium not in ('fresh','pressure'):
            raise ValueError('Пожарная линия: выберите СППВ или ППД')
        if not .2<=self.head_lead<=1000 or not .5<=self.offset<=1000 or not .05<=self.lane_gap<=5:
            raise ValueError('Недопустимое смещение магистрали или шаг труб')
        if any(getattr(self,k)<0 for k in ('corridor_weight','bend_penalty','crossing_penalty',
                                           'pipe_cost_per_m','elbow_cost','plate_cost','tee_cost','crossing_cost','corridor_saving_per_m')):
            raise ValueError('Стоимость не может быть отрицательной')
        if self.corridor_saving_per_m>self.pipe_cost_per_m/2+EPS:
            raise ValueError('Экономия коридора на метр не может превышать половину стоимости метра трубы: иначе оптимизация стимулирует лишнюю прокладку')
        if (self.center_x is None) != (self.center_y is None):
            raise ValueError('Для центральной точки нужно указать обе координаты X и Y')
        if self.center_x is not None and (not isinstance(self.center_x,(int,float)) or not isinstance(self.center_y,(int,float)) or not math.isfinite(self.center_x) or not math.isfinite(self.center_y)):
            raise ValueError('Некорректные координаты центральной точки')
        if not 1<=self.slab_step<=100 or not .2<=self.slab_axis_size<=self.slab_step:
            raise ValueError('Проверьте условный шаг и длину плит')
        if not 0<=self.branch_plate_offset<=100:
            raise ValueError('Отступ плиты отпайки от скважины должен быть в пределах 0–100 м')
        if not .05<=self.vertical_clearance<=10 or not .1<=self.bridge_approach<=30:
            raise ValueError('Проверьте отступ маркера пересечения (0,1–30 м)')
        if not 1<=len(self.wells)<=100 or not self.wells[0].on_axis:
            raise ValueError('Требуется от 1 до 100 скважин; первая должна быть на оси НДС')
        if len({w.number for w in self.wells})!=len(self.wells):
            raise ValueError('Номера скважин должны быть уникальны')
        for w in self.wells: w.validate()

    @classmethod
    def from_dict(cls,data):
        p=cls()
        for k in ('first_x','first_y','tie_x','tie_y','fire_x','fire_y','azimuth','coordinate_mode',
                  'head_lead','offset','lane_gap','auto_overpasses','vertical_clearance','bridge_approach','slab_step','slab_axis_size','branch_plate_offset','fire_medium','shared_corridors','tie_confirmed','fire_confirmed','center_x','center_y','corridor_weight','bend_penalty','crossing_penalty','trunk_side','center_position','auto_rotate',
                  'pipe_cost_per_m','elbow_cost','plate_cost','tee_cost','crossing_cost','corridor_saving_per_m'):
            if k in data and data[k] is not None: setattr(p,k,data[k])
        # 'tie_media' из проектов v3 читается, но намеренно игнорируется:
        # в v4 к точке врезки допускается ТОЛЬКО продуктовый нефтепровод.
        if data.get('version',0)<11:
            p.lane_gap=0.25  # конструктивный зазор хребта; отдельный транзитный зазор >=0.30 м
        # Старый параметр 1,5 м обозначал геометрию подъёма: теперь используем
        # половинный отступ 0,75 м только для условной 2D-метки.
        if 'version' in data and data['version']<26 and 'bridge_approach' in data:
            p.bridge_approach=float(data['bridge_approach']) / 2
        if 'wells' in data:p.wells=[Well(**{k:v for k,v in item.items() if k in Well.__dataclass_fields__}) for item in data['wells']]
        # v75: предыдущие версии выключали пучки принудительно.
        # В новых проектах выбор сохраняется и восстанавливается из JSON.
        if int(data.get('version',0)) < 75:
            p.shared_corridors=True
            if 'pipe_cost_per_m' in data and float(data['pipe_cost_per_m'])==1.0:
                p.pipe_cost_per_m=2000.0
            if 'elbow_cost' in data and float(data['elbow_cost'])==3.0:
                p.elbow_cost=4500.0
            if 'tee_cost' in data and float(data['tee_cost'])==4.0:
                p.tee_cost=6000.0
        # Legacy releases (through v79) saved a zero tee tariff as their
        # default. Migrate that legacy default to the new 6000 ₽ rate.
        # A deliberately entered zero in a v80+ project remains untouched.
        if int(data.get('version',0))<80 and float(data.get('tee_cost',0.0))==0.0:
            p.tee_cost=6000.0
        p.validate();return p
    def save(self,path):Path(path).write_text(json.dumps({'version':84,**asdict(self)},ensure_ascii=False,indent=2),encoding='utf-8')
    @classmethod
    def load(cls,path):return cls.from_dict(json.loads(Path(path).read_text(encoding='utf-8')))

class Frame:
    def __init__(self,p:Project):
        self.x0,self.y0=p.first_x,p.first_y
        a=math.radians(p.azimuth)
        if p.coordinate_mode=='x_north':
            self.du_x,self.du_y=math.cos(a),math.sin(a)
            self.dv_x,self.dv_y=math.sin(a),-math.cos(a)
        else:
            self.du_x,self.du_y=math.sin(a),math.cos(a)
            self.dv_x,self.dv_y=-math.cos(a),math.sin(a)
    def uv(self,x,y):
        dx,dy=x-self.x0,y-self.y0
        return Pt(dx*self.du_x+dy*self.du_y,dx*self.dv_x+dy*self.dv_y)
    def xy(self,q):return (self.x0+q.u*self.du_x+q.v*self.dv_x,
                           self.y0+q.u*self.du_y+q.v*self.dv_y)

@dataclass(frozen=True)
class Segment:
    a:Pt
    b:Pt
    medium:str
    phase:str
    purpose:str
    @property
    def length(self):return abs(self.b.u-self.a.u)+abs(self.b.v-self.a.v)
    @property
    def horizontal(self):return abs(self.a.v-self.b.v)<EPS

@dataclass(frozen=True)
class Slab:
    u_start:float
    u_end:float
    v_min:float
    v_max:float
    phase:str
    media_count:int
    media:tuple[str,...]
    slab_type:str='trunk'
    @property
    def u(self):
        """Среднее положение плиты для совместимости с ведомостью."""
        return (self.u_start+self.u_end)/2
    @property
    def length(self):
        return self.u_end-self.u_start
    @property
    def height(self):
        return self.v_max-self.v_min

@dataclass(frozen=True)
class WellPlacement:
    well:Well
    point:Pt
    world_x:float
    world_y:float
    connections:tuple
    orientation:str="parallel"
    connection_target:Pt|None=None   # Направление выхода к выбранному коридору; не перемещает скважину

@dataclass
class Result:
    project:Project
    frame:Frame
    wells:list[WellPlacement]
    center:Pt
    tie:Pt
    fire:Pt
    segments:list[Segment]
    slabs:list[Slab]
    lanes:dict[str,float]
    stats:dict[str,dict[str,float]]
    crossings:list[str]
    notes:list[str]
    head_u:float
    trunk_end_u:float
    tie_ports:dict[str,Pt]
    metrics:dict[str,float]=field(default_factory=dict)
    routing_groups:list[tuple[int,...]]=field(default_factory=list)
    trunk_side:int=1
    overpass_analysis:object|None=None  # архивное поле для совместимости с тестами v23; в v26 не заполняется
    crossing_markup:object|None=None  # только условная 2D-разметка пересечений
    selected_center_position:str='left_upper'
    central_plate:object|None=None
    decision_trace:dict=field(default_factory=dict)  # журнал реально рассмотренных вариантов


def axial_u(i):return (i//4)*(3*13.5+15.)+(i%4)*13.5


# v63: Фиксированная часть хребта не зависит от выбора транзитных маршрутов.
# 4 м после последней соответствующей осевой скважины — запас эскизной
# геометрии для независимых выходов и прямых участков. При необходимости
# расчёт может ПРОДЛИТЬ эту часть, но не перенести её на другую сторону.
BASE_TRUNK_END_MARGIN = 4.0

def base_axial_trunk_reaches(placements, direction=1):
    """Концы базовых ниток по осевым скважинам: (среда, фаза) -> U.

    direction=1 — центральная плита слева; -1 — справа. Внутри
    mirrored build для правой плиты применяется каноническое direction=1.
    """
    reaches = {}
    for wp in placements:
        if not wp.well.on_axis or wp.well.status == 'inactive':
            continue
        mediums = {m for m, _ in wp.connections if m != 'oil_measure'}
        if wp.well.kind == 'producer':
            mediums.add('oil_measure')
        for medium in mediums:
            phase = line_phase(wp, medium)
            u = wp.point.u + direction * BASE_TRUNK_END_MARGIN
            key = (medium, phase)
            if key not in reaches or direction*(u-reaches[key]) > 0:
                reaches[key] = u
    return reaches

def well_connections(w):
    if w.status=='inactive':return []
    data={
        'producer':[('fresh','от центра'),('oil_product','к центру')],
        'injector':[('pressure','от центра')],
        'ppd_source':[('pressure','к центру')],
        'fresh_source':[('fresh','к центру')],
    }[w.kind].copy()
    if w.gas:data.append(('gas','к центру'))
    return data

def add(out,a,b,m,ph,purpose):
    if abs(a.u-b.u)>EPS and abs(a.v-b.v)>EPS:
        raise AssertionError(f'Неортогональный участок {a} {b}')
    s=Segment(a,b,m,ph,purpose)
    if s.length>EPS:out.append(s)


def merge_segments(inp):
    """Убрать дубли метража одной среды, сохраняя назначение участков.

    Ранее наложение ветки на хребет объединялось в длинный отрезок с
    произвольным ``purpose``. Это ошибочно превращало магистральный участок
    в чужой транзит для проверки защитных зон. Делим только на реальных
    границах назначения и используем приоритет магистральной нитки.
    """
    groups={}
    for s in inp:
        if s.horizontal:
            key=(s.medium,s.phase,'u',round(s.a.v,6));a,b=sorted((s.a.u,s.b.u))
        else:
            key=(s.medium,s.phase,'v',round(s.a.u,6));a,b=sorted((s.a.v,s.b.v))
        groups.setdefault(key,[]).append((a,b,s.purpose))
    ans=[]
    for (m,ph,orientation,fixed),ranges in groups.items():
        bounds=sorted(set(round(q,8) for a,b,_ in ranges for q in (a,b)))
        pieces=[]
        for l,r in zip(bounds,bounds[1:]):
            if r-l<EPS:continue
            covered=[purpose for a,b,purpose in ranges if a<r-EPS and b>l+EPS]
            if not covered:continue
            # Магистральные участки имеют приоритет при совпадении с веткой
            # той же среды: они остаются исключением по осевым скважинам.
            # Плита — объект физической модели, поэтому её участок
            # обязателен в ведомости даже при совпадении оси с веткой.
            # Ранее ветка от скважины 10 перекрывала все 6 м СППВ на плите
            # и терялся тип центральной магистрали при слиянии.
            purpose=next((q for q in covered if q==PLATE_PURPOSE),None)
            if purpose is None:
                purpose=next((q for q in covered if q in ('Хребет','Перспективный хребет',
                                         'Перспективное продление хребта')),covered[0])
            if pieces and abs(pieces[-1][1]-l)<EPS and pieces[-1][2]==purpose:
                pieces[-1]=(pieces[-1][0],r,purpose)
            else:
                pieces.append((l,r,purpose))
        for a,b,purpose in pieces:
            aa,bb=(Pt(a,fixed),Pt(b,fixed)) if orientation=='u' else (Pt(fixed,a),Pt(fixed,b))
            add(ans,aa,bb,m,ph,purpose)
    return ans


def intersect(a,b):
    if a.horizontal!=b.horizontal:
        h,v=(a,b) if a.horizontal else (b,a)
        if min(h.a.u,h.b.u)+EPS<v.a.u<max(h.a.u,h.b.u)-EPS and min(v.a.v,v.b.v)+EPS<h.a.v<max(v.a.v,v.b.v)-EPS:
            return 'cross',Pt(v.a.u,h.a.v)
        return 'none',None
    if a.horizontal:
        if abs(a.a.v-b.a.v)>EPS:return 'none',None
        overlap=min(max(a.a.u,a.b.u),max(b.a.u,b.b.u))-max(min(a.a.u,a.b.u),min(b.a.u,b.b.u))
    else:
        if abs(a.a.u-b.a.u)>EPS:return 'none',None
        overlap=min(max(a.a.v,a.b.v),max(b.a.v,b.b.v))-max(min(a.a.v,a.b.v),min(b.a.v,b.b.v))
    return ('overlap',None) if overlap>EPS else ('none',None)


def forbidden_foreign_contacts(segments, head_u: float) -> list[str]:
    """Соприкосновение торца с другой средой — недопустимый ложный тройник.

    Обычное сквозное пересечение осей допускается как условная 2D-коллизия
    для последующей высотной проработки. Соприкосновения не являются таким
    переходом и не могут превращать две среды в одну сеть. В поперечном
    створе центра (head_u) расположены условные порты, там исключение.
    """
    found = {}
    for i, a in enumerate(segments):
        for b in segments[i+1:]:
            if a.medium == b.medium or a.horizontal == b.horizontal:
                continue
            h, v = (a, b) if a.horizontal else (b, a)
            point = Pt(v.a.u, h.a.v)
            if not (min(h.a.u, h.b.u)-EPS <= point.u <= max(h.a.u, h.b.u)+EPS
                    and min(v.a.v, v.b.v)-EPS <= point.v <= max(v.a.v, v.b.v)+EPS):
                continue
            # На створе центрального узла разные среды заканчиваются в
            # собственных условных портах; этим исключением нельзя
            # пользоваться в прочих местах площадки.
            if abs(point.u-head_u) < 1e-5:
                continue
            # На наружном торце центральной плиты продлеваемая магистраль
            # может пересекаться с независимой подводимой трубой. Это
            # считается УСЛОВНЫМ пересечением (верхняя — подводимая), а
            # не физическим тройником разных сред. Без собственного
            # шестиметрового участка плиты такое исключение запрещено.
            plate_face_crossing=any(
                ps.purpose==PLATE_PURPOSE and ps.medium in (a.medium,b.medium)
                and any(abs(q.u-point.u)<1e-5 and abs(q.v-point.v)<1e-5
                        for q in (ps.a,ps.b))
                for ps in segments)
            if plate_face_crossing:
                continue
            touch = any(abs(q.u-point.u)<1e-5 and abs(q.v-point.v)<1e-5
                        for q in (a.a, a.b, b.a, b.b))
            if touch:
                media = tuple(sorted((a.medium, b.medium)))
                key = (round(point.u, 5), round(point.v, 5), media)
                found[key] = (f'Недопустимое касание концов труб разных сред: '
                              f'{MEDIA_RU[media[0]]} / {MEDIA_RU[media[1]]}; '
                              f'U={point.u:.3f}, V={point.v:.3f} м')
    return list(found.values())


def collision_notes(segs):
    found=set()
    for i,a in enumerate(segs):
        for b in segs[i+1:]:
            if a.medium==b.medium:continue
            typ,pt=intersect(a,b)
            if typ=='cross':found.add(f'Пересечение {MEDIA_RU[a.medium]} / {MEDIA_RU[b.medium]}: U={pt.u:.2f}, V={pt.v:.2f} м')
            elif typ=='overlap':found.add(f'Наложение осей {MEDIA_RU[a.medium]} / {MEDIA_RU[b.medium]}')
    return sorted(found)




def add_plate(arr,u_start,u_end,v_min,v_max,phase,media,slab_type):
    if u_end-u_start<=EPS or v_max-v_min<=EPS:
        return
    arr.append(Slab(u_start,u_end,v_min,v_max,phase,len(tuple(media)),tuple(media),slab_type))

def segment_covered_on_interval(seg,u0,u1):
    if not seg.horizontal:
        return False
    a,b=sorted((seg.a.u,seg.b.u))
    return a<u1-EPS and b>u0+EPS

def build_trunk_slabs(p, lanes, trunk_segments, head_u, occupied_end):
    """Разреженная раскладка для линий хребта: плиты отдельно, не вплотную."""
    slabs=[]
    if occupied_end<=head_u+EPS:
        return slabs
    count=max(1,math.ceil((occupied_end-head_u)/p.slab_step))
    ordered=sorted(lanes,key=lambda m: lanes[m], reverse=True)
    for i in range(count):
        u0=head_u+i*p.slab_step
        u1=min(occupied_end,u0+p.slab_axis_size)
        if u1-u0<=EPS:
            continue
        here=[];active=[]
        for m in ordered:
            hit=[s for s in trunk_segments if s.medium==m and segment_covered_on_interval(s,u0,u1)]
            if not hit:
                continue
            here.append(m)
            if any(s.phase=='active' for s in hit):
                active.append(m)
        if not here:
            continue
        vs=[lanes[m] for m in here]
        add_plate(slabs,u0,u1,min(vs)-.58,max(vs)+.58,'active' if active else 'future',here,'trunk')
    return slabs

def build_branch_slabs(placements, lanes):
    """Плиты для отводов от скважины к хребту — отдельные вертикальные элементы."""
    slabs=[]
    for wp in placements:
        if wp.well.status=='inactive':
            continue
        ports=[med for med,_ in wp.connections]
        for m in ports:
            if m=='oil_measure':
                continue
            port_shift=(ports.index(m)-(len(ports)-1)/2)*1.3
            u=wp.point.u+port_shift
            v0,v1=sorted((wp.point.v,lanes[m]))
            if v1-v0<0.8:
                continue
            add_plate(slabs,u-1.2,u+1.2,v0-.6,v1+.6,wp.well.status,(m,), 'branch')
    return slabs

def build_transit_slabs(project, result_segments, lanes):
    """Разреженная раскладка плит для транзитных участков."""
    slabs=[]
    step=6.4
    length=2.4
    purposes=('Продуктовый нефтепровод: подход к врезке','Подключение пожарного узла')
    targets=[s for s in result_segments if s.purpose in purposes]
    for s in targets:
        if s.horizontal:
            a,b=sorted((s.a.u,s.b.u))
            cursor=a
            while cursor<b-EPS:
                u0=cursor
                u1=min(b,cursor+length)
                add_plate(slabs,u0,u1,s.a.v-.5,s.a.v+.5,s.phase,(s.medium,), 'transit')
                cursor+=step
        else:
            a,b=sorted((s.a.v,s.b.v))
            cursor=a
            while cursor<b-EPS:
                v0=cursor
                v1=min(b,cursor+length)
                add_plate(slabs,s.a.u-1.15,s.a.u+1.15,v0,v1,s.phase,(s.medium,), 'transit')
                cursor+=step
    return slabs



def shared_corridor_length(segments, clearance=2.1):
    """Приблизительная *протяжённость коридоров*, а не число/размеры плит.

    Отрезки разных труб, идущие параллельно достаточно близко, считаются
    участками ОДНОГО коридора. Суммируются продольные отрезки и поперечные;
    на пересечениях длина не удваивается по всей площади, оценка ориентировочная.
    """
    total=0.0
    for hor in (True,False):
        lines=[]
        for s in segments:
            # Пожарный трубопровод не участвует в подборе/оценке общих
            # коридоров со скважинами: отдельная система до её узла.
            if s.purpose.startswith('Подключение пожарного узла'):
                continue
            if s.horizontal != hor:
                continue
            a,b=sorted((s.a.u,s.b.u)) if hor else sorted((s.a.v,s.b.v))
            if b-a<EPS:
                continue
            lines.append((a,b,s.a.v if hor else s.a.u))
        if not lines:
            continue
        coords=sorted(set([a for a,_,_ in lines]+[b for _,b,_ in lines]))
        for a,b in zip(coords,coords[1:]):
            if b-a<EPS:
                continue
            fixes=sorted(f for l,r,f in lines if l < b-EPS and r > a+EPS)
            if not fixes:
                continue
            groups=1+sum(fixes[i+1]-fixes[i] > clearance+EPS for i in range(len(fixes)-1))
            total+=(b-a)*groups
    return total


def lane_positions(p:Project,side:int=1):
    """Order from the NDS axis outward, mirror entire bundle across NDS."""
    lanes={'oil_product':side*p.offset}
    previous='oil_product'
    for medium in ('gas','oil_measure','pressure','fresh'):
        lanes[medium]=lanes[previous]+side*(p.lane_gap+(PIPE_OD[previous]+PIPE_OD[medium])/2)
        previous=medium
    return lanes


WELL_LONG_SIDE=7.0/1.5    # Условный габарит схемного прямоугольника вдоль длинной стороны, м
WELL_SHORT_SIDE=4.0/2.5   # Условный габарит схемного прямоугольника поперёк длинной стороны, м
WELL_CONNECTION_STANDOFF=0.2  # Точка выхода чуть снаружи длинной стороны, м
WELL_MIN_STRAIGHT=2.0      # Минимальная прямая часть для осевых скважин, м
OFFAXIS_MIN_STRAIGHT=4.0  # 1 м отступ + 3 м цельная опорная плита


def long_side_direction(orientation:str)->tuple[int,int]:
    """Направление длинной стороны в координатах (U вдоль НДС, V поперёк НДС)."""
    if orientation=='parallel': return (1,0)
    if orientation=='perpendicular': return (0,1)
    raise ValueError(f'Недопустимая ориентация скважины: {orientation}')


def well_dimensions(wp:WellPlacement)->tuple[float,float]:
    """Половины размеров прямоугольника в метрах вдоль осей U и V."""
    if wp.orientation=='parallel':
        return (WELL_LONG_SIDE/2,WELL_SHORT_SIDE/2)
    return (WELL_SHORT_SIDE/2,WELL_LONG_SIDE/2)


def well_connection_normal(wp:WellPlacement,side:int=1,center:Pt|None=None)->Pt:
    """Единичная нормаль к длинной стороне, выбранная к магистрали или центру.

    Длинная сторона вдоль U -> выход вдоль V. Длинная сторона вдоль V -> вдоль U.
    Это НЕ ориентация направления бурения: ориентация теперь задаёт длинную сторону.
    """
    direction=wp.well.exit_direction
    if not wp.well.on_axis and direction!='auto':
        unit={'u+':Pt(1.,0.),'u-':Pt(-1.,0.),
              'v+':Pt(0.,1.),'v-':Pt(0.,-1.)}[direction]
        if (abs(unit.u)>EPS and wp.orientation!='perpendicular') or (abs(unit.v)>EPS and wp.orientation!='parallel'):
            raise ValueError(f'Скважина №{wp.well.number}: выбранный выход {direction} требует поворота прямоугольника')
        return unit
    if wp.orientation=='parallel':
        # Осевые скважины смотрят к хребту. Внеосевые стремятся к центральному узлу.
        if not wp.well.on_axis and center is not None and abs(center.v-wp.point.v)>EPS:
            return Pt(0.,1. if center.v>wp.point.v else -1.)
        return Pt(0.,1. if side>=0 else -1.)
    if center is not None and abs(center.u-wp.point.u)>EPS:
        return Pt(1. if center.u>wp.point.u else -1.,0.)
    return Pt(-1.,0.)


def well_outlet(wp:WellPlacement,side:int=1,center:Pt|None=None)->Pt:
    """Точка выхода отпаечной трубы на ДЛИННОЙ стороне прямоугольника.

    Параллельно НДС: длинная сторона идёт вдоль U, отвод отходит в +/-V.
    Перпендикулярно НДС: длинная сторона идёт вдоль V, отвод отходит в +/-U.
    """
    n=well_connection_normal(wp,side,wp.connection_target or center)
    hu,hv=well_dimensions(wp)
    return Pt(wp.point.u+n.u*(hu if abs(n.u)>EPS else hv)+n.u*WELL_CONNECTION_STANDOFF,
              wp.point.v+n.v*(hu if abs(n.u)>EPS else hv)+n.v*WELL_CONNECTION_STANDOFF)


def bundle_offsets(media,gap=.25):
    media=sorted(set(media),key=MEDIA.index)
    if not media:return {}
    xs=[0.]
    for a,b in zip(media,media[1:]):
        xs.append(xs[-1]+gap+(PIPE_OD[a]+PIPE_OD[b])/2)
    center=(xs[0]+xs[-1])/2
    return {m:xs[i]-center for i,m in enumerate(media)}




def well_medium_outlets(wp:WellPlacement,side:int=1,center:Pt|None=None,
                        gap:float=.25,reverse_tangent:bool=False)->dict[str,Pt]:
    """Самостоятельные выходы на длинной стороне устья для каждой среды.

    Центры соседних труб разнесены на свободный зазор плюс их полудиаметры.
    Все первые 2 метра идут параллельно нормали к длинной стороне.
    """
    media=[m for m,_ in wp.connections if m!='oil_measure']
    offsets=bundle_offsets(media,gap)
    origin=well_outlet(wp,side,center)
    # При внутреннем развороте координат для ПРАВОЙ центральной плиты
    # тангенциальный порядок портов обращаем; физические точки устья
    # после возврата к исходным координатам должны остаться прежними.
    factor=-1 if reverse_tangent else 1
    if wp.orientation=='parallel':
        return {m:Pt(origin.u+factor*offsets[m],origin.v) for m in media}
    return {m:Pt(origin.u,origin.v+factor*offsets[m]) for m in media}

def line_phase(wp:WellPlacement, medium:str)->str:
    """Фаза отдельной линии.

    Для активной скважины отдельные линии могут быть отмечены как
    перспективные и тогда рисуются пунктиром. Для неактивных/перспективных
    скважин приоритет имеет общий статус скважины.
    """
    if wp.well.status!='active':
        return wp.well.status
    return 'future' if medium in getattr(wp.well,'planned_media',[]) else 'active'


def bundle_offsets_for_lines(line_keys,gap=.25):
    """Смещения для набора отдельных линий в общем коридоре.

    В отличие от ``bundle_offsets`` дубликаты одной и той же среды сохраняются:
    две независимые линии не накладываются, даже если у них одинаковая среда.
    Порядок — как у хребта: по типу среды, внутри типа по номеру скважины.
    """
    ordered=list(line_keys)
    if not ordered:
        return {}
    xs=[0.0]
    for prev,curr in zip(ordered,ordered[1:]):
        a=prev[1] if isinstance(prev,tuple) else prev
        b=curr[1] if isinstance(curr,tuple) else curr
        xs.append(xs[-1]+gap+(PIPE_OD[a]+PIPE_OD[b])/2)
    center=(xs[0]+xs[-1])/2
    return {k:xs[i]-center for i,k in enumerate(ordered)}



def distance_segment_to_point(a:Pt,b:Pt,c:Pt)->float:
    """Точное минимальное расстояние в плоскости U/V до отрезка."""
    dx=b.u-a.u; dy=b.v-a.v
    den=dx*dx+dy*dy
    if den<EPS*EPS:
        return math.hypot(a.u-c.u,a.v-c.v)
    f=max(0.,min(1.,((c.u-a.u)*dx+(c.v-a.v)*dy)/den))
    return math.hypot(a.u+f*dx-c.u,a.v+f*dy-c.v)


def no_immediate_uturn(points):
    """Disallow a 180-degree reversal/backtracking on a pipe centerline.

    A sharp return along the same axis is not a 90-degree elbow; it would
    double-count pipe on overlapping segments and has no buildable fitting.
    """
    for a,b,c in zip(points,points[1:],points[2:]):
        du1,dv1=b.u-a.u,b.v-a.v
        du2,dv2=c.u-b.u,c.v-b.v
        if (abs(du1)+abs(dv1)>EPS and abs(du2)+abs(dv2)>EPS
                and abs(du1*dv2-dv1*du2)<1e-7
                and du1*du2+dv1*dv2< -EPS):
            return False
    return True


def trunk_inner_zone(lanes, side, head_u, placements):
    """Продольный запретный коридор со стороны осевых скважин.

    Полоса охватывает ось НДС и параллельные хребтовые нитки по всей длине
    основного ряда. Для транзитных труб нельзя прокладывать в ней участки
    вдоль U; поперечные короткие подходы к портам не запрещены этой проверкой.
    Отдельный контроль десятиметровых окружностей остаётся обязательным.
    """
    if side not in (-1, 1) or not lanes:
        return None
    axis=[w.point.u for w in placements if w.well.on_axis]
    if not axis:
        return None
    # Все пять осей входят в один хребет. Резервируем небольшой зазор
    # с наружной стороны крайней нитки, чтобы путь не шёл вплотную к ней.
    outer=max(side * v for v in lanes.values())+0.35
    first,last=min(axis),max(axis)
    # Хребет тянется между внутренними торцами левой и правой
    # альтернатив центральной плиты. Не распространяем ограничение на
    # ВНЕШНИЙ торец самой плиты: там начинается пожарный вывод.
    if head_u <= first:
        left=head_u
        right=last+(first-head_u)
    elif head_u >= last:
        left=first-(head_u-last)
        right=head_u
    else:
        left,right=first,last
    return (side,outer,left,right)


def inner_trunk_parallel(a, b, zone):
    """Есть ли у отрезка запрещённый продольный участок в полосе?"""
    if zone is None or abs(a.v-b.v)>EPS or abs(a.u-b.u)<EPS:
        return False
    sign, outer, left, right=zone
    distance=sign*a.v
    if distance < -EPS or distance >= outer-EPS:
        return False
    lo,hi=sorted((a.u,b.u))
    return min(right,hi)-max(left,lo)>EPS


def inner_trunk_transit_conflicts(segs, placements, lanes, side, head_u):
    """Единая окончательная проверка ВСЕХ линий, включая результат пучков.

    Нельзя обойти ограничение использованием старого маршрутизатора или
    постобработки. Исключения: сам хребет, трубы внутри центральной плиты,
    короткие технологические ответвления от осевых скважин.
    """
    zone=trunk_inner_zone(lanes,side,head_u,placements)
    axial={w.well.number for w in placements if w.well.on_axis}
    conflicts=[]
    for seg in segs:
        if seg.purpose in ('Хребет','Перспективный хребет',
                           'Перспективное продление хребта',PLATE_PURPOSE,
                           'Условный вывод замерной линии'):
            continue
        owners=set(map(int,re.findall(r'скв\.\s*(\d+)',seg.purpose)))
        if seg.purpose.startswith('Отвод от скв.') and owners and owners<=axial:
            continue
        if inner_trunk_parallel(seg.a,seg.b,zone):
            conflicts.append(seg)
    return conflicts


def protected_well_conflicts(segs,placements,radius=WELL_GUARD_RADIUS):
    """Контроль всей сети по защитным зонам, включая необустраиваемые скважины.
    Разрешение входа действует ТОЛЬКО для подключения к скважине-владельцу.
    Продольный хребет вдоль оси НДС не считается нарушением для осевых скважин,
    иначе при увеличенном радиусе схема становится заведомо неработоспособной.
    """
    found=set()
    for seg in segs:
        owners=set(map(int,re.findall(r'скв\.\s*(\d+)',seg.purpose)))
        trunk_like=('Хребет' in seg.purpose or 'Перспективный хребет' in seg.purpose
                    or 'Перспективное продление хребта' in seg.purpose
                    or 'Условный вывод замерной линии' in seg.purpose)
        for wp in placements:
            if wp.well.number in owners:
                continue
            if trunk_like and wp.well.on_axis and seg.horizontal:
                continue
            if distance_segment_to_point(seg.a,seg.b,wp.point)<radius-1e-5:
                found.add((wp.well.number,seg.medium,seg.purpose))
    return sorted(found)


def protected_line_ok(a,b,obstacles,owner=None,radius=WELL_GUARD_RADIUS):
    for w in obstacles:
        if w.well.number==owner:continue
        if distance_segment_to_point(a,b,w.point)<radius-1e-6:
            return False
    return True


def rectilinear_safe_path(start:Pt,goal:Pt,placements,owner=None,bend_penalty=3.0,
                          existing=(),medium=None,inner_zone=None,
                          incoming:Pt|None=None,outgoing:Pt|None=None):
    """Поиск безопасной ортогональной трассы на видимостной сетке.

    Построение локального множества линий по краям запретных окружностей,
    затем Дейкстра с учётом длины, поворотов и чужих трубопроводов.
    Любой выбранный отрезок проверяется геометрически, а не по клеткам растра.
    """
    if abs(start.u-goal.u)<EPS and abs(start.v-goal.v)<EPS:return [start]
    # Выбор пути должен учитывать реальные отводы НА СТЫКАХ: от уже
    # существующего прямого выхода скважины и к финальному боковому порту.
    # Иначе два равно-кратчайших Г-пути получают одинаковую ложную цену,
    # хотя один фактически требует до двух дополнительных отводов.
    def attachment_turns(nodes):
        extended=list(nodes)
        if incoming is not None:
            extended.insert(0,Pt(nodes[0].u-incoming.u,nodes[0].v-incoming.v))
        if outgoing is not None:
            extended.append(Pt(nodes[-1].u+outgoing.u,nodes[-1].v+outgoing.v))
        if not no_immediate_uturn(extended):
            return None
        return sum(1 for a,b,c in zip(extended,extended[1:],extended[2:])
                   if (abs(b.u-a.u)<EPS)!=(abs(c.u-b.u)<EPS))
    # Быстрый точный тест двух маршрутов с одним поворотом. В большинстве
    # случаев он заменяет тяжёлый поиск на сетке без потери качества по длине.
    options=((start,Pt(start.u,goal.v),goal),
             (start,Pt(goal.u,start.v),goal))
    valid=[]
    for route in options:
        nodes=[q for i,q in enumerate(route) if i==0 or q!=route[i-1]]
        if any((not protected_line_ok(a,b,placements,owner=owner)
                or inner_trunk_parallel(a,b,inner_zone))
               for a,b in zip(nodes,nodes[1:])):
            continue
        overlaps=0;crosses=0
        for a,b in zip(nodes,nodes[1:]):
            new_seg=Segment(a,b,medium or 'fresh','active','Предварительный путь')
            for s in existing:
                if s.medium==medium:continue
                kind,_=intersect(new_seg,s)
                if kind=='overlap':overlaps+=1
                elif kind=='cross':crosses+=1
        turns=attachment_turns(nodes)
        if turns is None:continue
        span=sum(abs(b.u-a.u)+abs(b.v-a.v) for a,b in zip(nodes,nodes[1:]))
        cost=span+bend_penalty*turns+10000*overlaps+3*crosses
        valid.append((cost,span,turns,overlaps,tuple(nodes)))
    if valid:
        valid.sort(key=lambda item:(round(item[0],7),round(item[1],7),
                                    item[2],item[3],tuple((v.u,v.v) for v in item[4])))
        if valid[0][3]==0:
            return list(valid[0][4])
    radius=WELL_GUARD_RADIUS+GUARD_BUFFER
    margin=radius*3+5.
    min_u,max_u=sorted((start.u,goal.u));min_v,max_v=sorted((start.v,goal.v))
    obstacles=[wp for wp in placements if wp.well.number!=owner and
               min_u-margin<=wp.point.u<=max_u+margin and min_v-margin<=wp.point.v<=max_v+margin]
    for pt in (start,goal):
        for wp in obstacles:
            if math.hypot(pt.u-wp.point.u,pt.v-wp.point.v)<WELL_GUARD_RADIUS-1e-5:
                raise ValueError(f'Нельзя проложить трубопровод: точка ({pt.u:.1f}; {pt.v:.1f}) '
                                 f'попала в 5-метровую защитную зону скважины №{wp.well.number}')
    xx={start.u,goal.u}
    yy={start.v,goal.v}
    if inner_zone is not None:
        sign,outer,edge0,edge1=inner_zone
        xx.update((round(edge0-0.5,5),round(edge1+0.5,5)))
        yy.add(round(sign*(outer+0.50),5))
    for wp in obstacles:
        xx.update((round(wp.point.u-radius,5),round(wp.point.u+radius,5)))
        yy.update((round(wp.point.v-radius,5),round(wp.point.v+radius,5)))
    # Необходимый резерв на объезд даже если два конца имеют общую координату.
    xx.update((round(min_u-margin,5),round(max_u+margin,5)))
    yy.update((round(min_v-margin,5),round(max_v+margin,5)))
    X=sorted(xx);Y=sorted(yy);nx=len(X);ny=len(Y)
    sx=X.index(start.u);sy=Y.index(start.v)
    tx=X.index(goal.u);ty=Y.index(goal.v)
    # Эффективность: короткие ребра только между соседними линиями сетки.
    # Состояние хранит направление предыдущего ребра (0=первое, 1=U, 2=V).
    initial_axis=(1 if abs(incoming.u)>EPS else 2) if incoming is not None else 0
    heap=[(0.,0.,0.,sx,sy,initial_axis)]
    best={(sx,sy,initial_axis):(0.,0,0.)};parent={}
    end_state=None
    end_quality=(math.inf,math.inf,math.inf)
    while heap:
        cost,bends,length,i,j,direction=heapq.heappop(heap)
        state=(i,j,direction)
        if (cost,bends,length)>best.get(state,(math.inf,math.inf,math.inf)):
            continue
        if cost>end_quality[0]+1e-7:break
        if (i,j)==(tx,ty):
            nodes_tail=[]
            current=state
            while current in parent:
                previous=parent[current]
                nodes_tail.append((previous,current))
                current=previous
            if outgoing is not None and nodes_tail:
                prev_pos,final_pos=nodes_tail[0]
                move=Pt(X[final_pos[0]]-X[prev_pos[0]],
                        Y[final_pos[1]]-Y[prev_pos[1]])
                if abs(move.u*outgoing.v-move.v*outgoing.u)<1e-7 and (move.u*outgoing.u+move.v*outgoing.v)<-EPS:
                    continue
            extra=int(outgoing is not None and direction != (1 if abs(outgoing.u)>EPS else 2))
            quality=(cost+bend_penalty*extra,bends+extra,length)
            if quality<end_quality:
                end_quality=quality;end_state=state
            continue
        for di,dj,new_dir in ((1,0,1),(-1,0,1),(0,1,2),(0,-1,2)):
            ii=i+di;jj=j+dj
            if not(0<=ii<nx and 0<=jj<ny):continue
            a=Pt(X[i],Y[j]);b=Pt(X[ii],Y[jj])
            if (not protected_line_ok(a,b,obstacles,radius=WELL_GUARD_RADIUS)
                or inner_trunk_parallel(a,b,inner_zone)):continue
            span=abs(a.u-b.u)+abs(a.v-b.v)
            turning=(direction!=0 and direction!=new_dir)
            # Запрет обратного движения вдоль первоначального выхода скважины.
            if state==(sx,sy,initial_axis) and incoming is not None:
                if (b.u-a.u)*incoming.u+(b.v-a.v)*incoming.v < -EPS:
                    continue
            extra=0.
            # Предпочтение использовать существующую трассу своей среды,
            # но запрещаем наложение чужих труб вне зоны самого подключения.
            if existing:
                for s in existing:
                    typ,_=intersect(Segment(a,b,medium or s.medium,'active','Проверка'),s)
                    if s.medium!=medium:
                        if typ=='overlap':extra+=10000.
                        elif typ=='cross':extra+=3.
            cc=cost+span+(bend_penalty if turning else 0.)+extra
            ll=length+span
            next_state=(ii,jj,new_dir)
            quality=(cc,bends+int(turning),ll)
            if quality<best.get(next_state,(math.inf,math.inf,math.inf)):
                best[next_state]=quality
                parent[next_state]=state
                heapq.heappush(heap,(cc,bends+int(turning),ll,ii,jj,new_dir))
    if end_state is None:
        raise ValueError(f'Нет безопасной ортогональной трассы от ({start.u:.1f}; {start.v:.1f}) '
                         f'до ({goal.u:.1f}; {goal.v:.1f}) при радиусе 5 м')
    states=[end_state]
    while states[-1] in parent:states.append(parent[states[-1]])
    nodes=[Pt(X[i],Y[j]) for i,j,_ in reversed(states)]
    # Сжимаем коллинеарные проходные точки, сохранив конечные точки.
    compact=[]
    for pt in nodes:
        compact.append(pt)
        while len(compact)>=3:
            a,b,c=compact[-3:]
            if (abs(a.u-b.u)<EPS and abs(b.u-c.u)<EPS) or (abs(a.v-b.v)<EPS and abs(b.v-c.v)<EPS):
                compact.pop(-2)
            else:break
    if any(inner_trunk_parallel(a,b,inner_zone) for a,b in zip(compact,compact[1:])):
        raise ValueError('Не найден обход продольного коридора со стороны скважин')
    return compact


def route_safe_external(segs,placements,start,endpoint,medium,phase,purpose,bend_penalty):
    path=rectilinear_safe_path(start,endpoint,placements,bend_penalty=bend_penalty,
                              existing=segs,medium=medium)
    for a,b in zip(path,path[1:]):add(segs,a,b,medium,phase,purpose)
    return sum(1 for a,b,c in zip(path,path[1:],path[2:]) if
               (abs(a.u-b.u)<EPS)!=(abs(b.u-c.u)<EPS))

def route_of_well(wp,lanes,riser_u=None,riser_offsets=None,line_offsets=None,head_u=-1e12,side=1,gap=.25,
                  all_wells=None,staging_v=None,bend_penalty=3.0,existing=(),trunk_reach=None,
                  central_plate=None,crossing_penalty=3.0,plate_media=None,fire_reservation=None,decision_sink=None,
                  mirror_tangent:bool=False):
    """Трассировка подключения отдельной скважины.

    Для внеосевых скважин обязателен прямой участок не менее 2 м от устья
    до первого поворота. Если есть существующий участок хребта той же среды,
    сначала ищется ближайшее допустимое присоединение к нему, а не ко входу
    хребта у центрального узла. Если задан ``riser_u``, применяется явно
    выбранный общий коридор, который дополнительно сравнивается по стоимости.
    """
    out=[];ports={};bends=0
    media=[m for m,_ in wp.connections if m!='oil_measure']
    # Осевые скважины не участвуют в выборе групп/пучков и всегда
    # используют собственные фиксированные устьевые порты.
    offsets=bundle_offsets(media,gap) if (riser_u is None or wp.well.on_axis) else (riser_offsets or {})
    line_offsets=line_offsets or {}
    center_hint=Pt(head_u,lanes['oil_measure'])
    outlets=well_medium_outlets(wp,side,center=center_hint,gap=gap,
                                reverse_tangent=(mirror_tangent and wp.well.on_axis))
    normal=well_connection_normal(wp,side,center_hint)
    inner_zone=trunk_inner_zone(lanes,side,head_u,all_wells or ())

    def conflicts_with_reserved_fire(segments, medium):
        """Пожарный вывод самостоятельный: параллельные водяные ветви
        не занимают полосу, зарезервированную для его прямого выхода.
        За точечное пересечение не штрафуем здесь — оно анализируется отдельно.
        """
        if fire_reservation is None:
            return False
        fire_routes=fire_reservation[1]
        clearance=max(1.0,gap+(PIPE_OD[medium]+PIPE_OD[fire_reservation[0]])/2+.15)
        for a in segments:
            for b in fire_routes:
                if a.horizontal != b.horizontal:
                    continue
                if a.horizontal:
                    spacing=abs(a.a.v-b.a.v)
                    al,ah=sorted((a.a.u,a.b.u));bl,bh=sorted((b.a.u,b.b.u))
                else:
                    spacing=abs(a.a.u-b.a.u)
                    al,ah=sorted((a.a.v,a.b.v));bl,bh=sorted((b.a.v,b.b.v))
                if spacing < clearance-1e-5 and min(ah,bh)-max(al,bl)>3.0:
                    return True
        return False

    for m in media:
        inspected=[]
        rejected_counts={}
        def reject(reason):
            rejected_counts[reason]=rejected_counts.get(reason,0)+1
        def record_choice(chosen_kind, chosen_score=None, chosen_route=None):
            if decision_sink is None or wp.well.on_axis:
                return
            key=(wp.well.number,m)
            # This is the local route candidate list, not a globally proven optimum.
            decision_sink[key]={
                'source':'local_route_of_well', 'well':wp.well.number, 'medium':m,
                'orientation':wp.orientation,'choice':chosen_kind,
                'choice_cost':chosen_score,
                'choice_path':[(q.u,q.v) for q in chosen_route] if chosen_route else [],
                'alternatives':sorted(inspected,key=lambda x:(x['score'],x['kind'])),
                'rejected':dict(rejected_counts),
                'note':'Локальная оценка учитывает длину, повороты, пересечения и препятствия; окончательная стоимость J всей сети может отличаться.',
            }
        outlet=outlets[m]
        initial_straight=WELL_MIN_STRAIGHT if wp.well.on_axis else OFFAXIS_MIN_STRAIGHT
        lead=Pt(outlet.u+normal.u*initial_straight, outlet.v+normal.v*initial_straight)
        v=lanes[m]
        if wp.well.on_axis or all_wells is None:
            # Трубы уже получили независимые выходы. Вдоль НДС не
            # добавляем смещение второй раз: это сохраняет заданный шаг.
            if wp.well.on_axis:
                # БЛИЖАЙШАЯ проекция единственного устьевого выхода на
                # прямую магистраль данной среды. Групповой riser_u и
                # произвольные смещения НЕ имеют права её изменить.
                uport=max(head_u,lead.u)
            elif wp.orientation=='parallel':
                uport=max(head_u, lead.u if riser_u is None else riser_u+offsets.get(m,0))
            else:
                uport=max(head_u, lead.u+offsets.get(m,0))
            purpose=f'Отвод от скв. {wp.well.number}'
            phase=line_phase(wp,m)
            add(out,outlet,lead,m,phase,purpose)
            add(out,lead,Pt(uport,lead.v),m,phase,purpose)
            add(out,Pt(uport,lead.v),Pt(uport,v),m,phase,purpose)
            if abs(lead.u-uport)>EPS and abs(lead.v-v)>EPS:bends+=1
            ports[m]=uport
            continue
        # v36: базовое подключение внеосевой скважины непосредственно к
        # существующей хребтовой нитке СВОЕЙ среды. Прежде программа всегда
        # направляла ветку к U=head_u, хотя нитка нередко находится под скважиной.
        # Для действующей ветки нельзя использовать лишь перспективную трубу.
        reach=(trunk_reach or {}).get(m)
        # Внеосевая транзитная ветвь может подойти к длинной боковой грани
        # центральной плиты. Хребет по-прежнему входит ТОЛЬКО в её торец,
        # как и независимый пожарный трубопровод. Порт здесь — точка
        # присоединения к собственной трубе среды внутри плиты, а не место
        # пересечения с чужой магистралью (чужие линии пересекаются сверху).
        #
        # Сравниваем боковой вариант с хребтом одинаковой метрикой; никакого
        # обязательного предпочтения «всё с торца» быть не должно.
        side_candidates=[]
        if central_plate is not None and (riser_u is None or m in SIDE_ONLY_PLATE_MEDIA):
            pad=0.35
            low=central_plate.u_min+pad
            high=central_plate.u_max-pad
            # All build-time side ports use the same 0.1 m lattice as
            # the preview and the product tie. There is NO plate-end option.
            from product_tie_rules import tick_values
            # Only the face on the actual well's side of the central plate.
            face_sign=(1 if wp.point.v>=central_plate.center_v else -1)
            edge_v=(central_plate.v_max if face_sign==1 else central_plate.v_min)
            ux=tick_values(low,high)
            # Lower-bound ordering permits safe pruning while preserving
            # the best local side-port choice, not just four arbitrary ports.
            ux.sort(key=lambda u:(abs(u-lead.u),u))
            for u in ux:
                lower=abs(u-lead.u)+abs(edge_v-lead.v)
                if side_candidates and lower>min(q[0] for q in side_candidates)+EPS:
                    continue
                # Approach from the source-facing side, NEVER along a plate end.
                gate=Pt(u,edge_v+face_sign*.35)
                terminal=Pt(u,v)
                try:
                    approach=rectilinear_safe_path(lead,gate,all_wells,
                                owner=wp.well.number,bend_penalty=bend_penalty,
                                existing=tuple(existing)+tuple(out),medium=m,
                                inner_zone=inner_zone,
                                incoming=Pt(lead.u-outlet.u,lead.v-outlet.v),
                                outgoing=Pt(terminal.u-gate.u,terminal.v-gate.v))
                    if not protected_line_ok(gate,terminal,all_wells,
                                             owner=wp.well.number):
                        reject('Боковой ввод на плиту пересекает защитную зону')
                        continue
                except ValueError:
                    continue
                route=[outlet]+approach
                if route[-1] != terminal:
                    route.append(terminal)
                if not no_immediate_uturn(route):
                    reject('Маршрут разворачивается на 180 градусов возле скважины')
                    continue
                turns=sum(1 for a,b,c in zip(route,route[1:],route[2:])
                          if (abs(a.u-b.u)<EPS)!=(abs(b.u-c.u)<EPS))
                distance=sum(abs(b.u-a.u)+abs(b.v-a.v) for a,b in zip(route[1:],route[2:]))
                contacts=0
                for a,b in zip(route[1:],route[2:]):
                    draft=Segment(a,b,m,'active','Проверка бокового входа')
                    for old in existing:
                        if old.medium==m:continue
                        relation,_=intersect(draft,old)
                        if relation=='overlap':contacts+=10000
                        elif relation=='cross':contacts+=crossing_penalty
                # Переходы внутри плиты допустимы, но не бесплатны.
                cross_lanes=sum(1 for other in (plate_media if plate_media is not None else lanes)
                                if other!=m and min(edge_v,v)+EPS<lanes[other]<max(edge_v,v)-EPS)
                cost=distance+bend_penalty*turns+contacts+crossing_penalty*cross_lanes
                # Рядом с торцом возможен отдельный прямой пожарный
                # трубопровод. Сначала проверяем простой подход, но не
                # допускаем плотного параллельного наложения на пожарный.
                trial=[Segment(a,b,m,'active','Боковой транзит')
                       for a,b in zip(route,route[1:]) if abs(a.u-b.u)+abs(a.v-b.v)>EPS]
                if not conflicts_with_reserved_fire(trial,m):
                    side_candidates.append((cost,distance,turns,abs(u-lead.u),u,route))
                    if decision_sink is not None:
                        inspected.append({'kind':'Боковая грань плиты', 'target':(u,edge_v),
                                          'score':round(cost,5),'length':round(distance,5),
                                          'bends':turns,'result':'допустим'})
                elif fire_reservation is not None:
                    reject('Прямой боковой вход конфликтует с зарезервированной пожарной линией')
                    # Обход ИМЕННО транзита, а не пожарной магистрали:
                    # короткий сдвиг за длинную грань плиты. Соединение
                    # на плите остаётся боковым, 2D-геометрия ортогональна.
                    clearance=max(1.0,gap+PIPE_OD[m])+0.10
                    outer_v=edge_v+(clearance if edge_v==central_plate.v_max else -clearance)
                    stage=Pt(u,outer_v)
                    from_start=Pt(lead.u,outer_v)
                    shifted=[outlet,lead,from_start,stage,gate,terminal]
                    shifted=[q for i,q in enumerate(shifted) if i==0 or q!=shifted[i-1]]
                    if all(abs(a.u-b.u)<EPS or abs(a.v-b.v)<EPS
                           for a,b in zip(shifted,shifted[1:])):
                        shifted_segments=[Segment(a,b,m,'active','Сдвинутый транзит')
                                          for a,b in zip(shifted,shifted[1:]) if a!=b]
                        if (not conflicts_with_reserved_fire(shifted_segments,m) and
                            all(protected_line_ok(a,b,all_wells,owner=wp.well.number)
                                for a,b in zip(shifted,shifted[1:]))):
                            new_bends=sum(1 for a,b,c in zip(shifted,shifted[1:],shifted[2:])
                                           if (abs(a.u-b.u)<EPS)!=(abs(b.u-c.u)<EPS))
                            new_length=sum(abs(a.u-b.u)+abs(a.v-b.v)
                                           for a,b in zip(shifted,shifted[1:]))
                            new_cost=new_length+bend_penalty*new_bends+contacts+crossing_penalty*cross_lanes
                            side_candidates.append((new_cost,new_length,new_bends,abs(u-lead.u),u,shifted))
                            if decision_sink is not None:
                                inspected.append({'kind':'Боковой порт с обходом','target':(u,edge_v),
                                                  'score':round(new_cost,5),'length':round(new_length,5),
                                                  'bends':new_bends,'result':'допустим'})
        if riser_u is None and reach is not None and reach > head_u + EPS:
            from product_tie_rules import (projected_trunk_sites,
                     object_on_trunk_external_side)
            phase=line_phase(wp,m)
            sites=projected_trunk_sites(all_wells,central_plate,lanes,
                                        m,head_u,reach,phase)
            sites.sort(key=lambda site:(abs(site.point.u-lead.u),
                        0 if site.kind=='side' else 1,site.point.u))
            candidates=[];visited=set()
            for site in sites:
                if (site.kind=='side' and not
                    object_on_trunk_external_side(wp.point.v,v,side)):
                    continue
                port_u=round(site.point.u,6)
                if site.kind!='end' and port_u <= head_u+1e-5:
                    reject('Торцевое подключение центральной плиты запрещено')
                    continue
                if port_u in visited:continue
                visited.add(port_u)
                endpoint_trial=Pt(port_u,v)
                from topology_rules import external_trunk_gate, external_trunk_approach
                # Боковые ответвления ВНЕОСЕВЫХ скважин присоединяются к
                # хребту со стороны ОТ НДС, не как осевые фиксированные отпайки.
                if site.kind=='end':
                    # Only the opposite FREE end of the existing trunk.
                    end_sign=1 if port_u>=central_plate.trunk_face_u else -1
                    gate=Pt(port_u+end_sign*.65,v)
                else:
                    gate=external_trunk_gate(endpoint_trial,side,
                                clearance=max(0.65,gap+PIPE_OD[m]+0.25))
                if candidates and (abs(gate.u-lead.u)+abs(gate.v-lead.v)) > min(q[0] for q in candidates)+EPS:
                    continue
                try:
                    preceding=rectilinear_safe_path(lead,gate,all_wells,
                                owner=wp.well.number,bend_penalty=bend_penalty,
                                existing=tuple(existing)+tuple(out),medium=m,
                        inner_zone=inner_zone,
                        incoming=Pt(lead.u-outlet.u,lead.v-outlet.v),
                        outgoing=Pt(endpoint_trial.u-gate.u,endpoint_trial.v-gate.v))
                    if not protected_line_ok(gate,endpoint_trial,all_wells,
                                             owner=wp.well.number):
                        reject('Внешний подвод к хребту пересекает охранную зону')
                        continue
                    trail=[*preceding,endpoint_trial]
                    if not no_immediate_uturn([outlet,*trail]):
                        reject('Маршрут разворачивается на 180 градусов')
                        continue
                    if site.kind=='end':
                        if not (abs(trail[-2].v-v)<1e-5 and
                                (trail[-2].u-port_u)*end_sign>0):
                            reject('Торцевое подключение допускается только снаружи')
                            continue
                    elif not external_trunk_approach(trail,v,side):
                        reject('Подключение с внутренней стороны хребта запрещено')
                        continue
                except ValueError:
                    reject('Не найден безопасный подвод к хребту с внешней стороны')
                    continue
                if site.kind!='end' and m in SIDE_ONLY_PLATE_MEDIA and central_plate is not None:
                    # Нельзя считать «подключением к хребту» путь, который
                    # сначала заходит в тело центральной плиты, а затем
                    # проходит её внутренний торец по оси собственной трубы.
                    if any(abs(aa.v-v)<1e-5 and abs(bb.v-v)<1e-5 and
                           min(aa.u,bb.u)<head_u-1e-5 and
                           max(aa.u,bb.u)>head_u+1e-5
                           for aa,bb in zip(trail,trail[1:])):
                        reject('Маршрут проходит сквозь торец центральной плиты')
                        continue
                # Транзит нельзя присоединять к зарезервированной прямой
                # пожарной линии или прокладывать соосно с ней: иначе
                # пожарному узлу позже приходится делать обход.
                trial_for_fire=[Segment(a,b,m,'active','Кандидат транзита')
                                for a,b in zip([outlet]+trail,trail)
                                if a!=b]
                if conflicts_with_reserved_fire(trial_for_fire,m):
                    reject('Транзит занимает независимый пожарный коридор')
                    continue
                nodes=[outlet]+trail
                turns=sum(1 for aa,bb,cc in zip(nodes,nodes[1:],nodes[2:])
                          if (abs(aa.u-bb.u)<EPS)!=(abs(bb.u-cc.u)<EPS))
                distance=sum(abs(bb.u-aa.u)+abs(bb.v-aa.v) for aa,bb in zip(trail,trail[1:]))
                # Сначала минимальная реальная длина + повороты, затем
                # точные совпадения/пересечения с уже проложенными сетями.
                contacts=0
                for aa,bb in zip(trail,trail[1:]):
                    draft=Segment(aa,bb,m,'active','Проверка присоединения')
                    for old in existing:
                        if old.medium==m:continue
                        relation,_=intersect(draft,old)
                        if relation=='overlap':contacts+=10000
                        elif relation=='cross':contacts+=crossing_penalty
                cost=distance+bend_penalty*turns+contacts
                candidates.append((cost,distance,turns,abs(port_u-lead.u),port_u,trail))
                if decision_sink is not None:
                    inspected.append({'kind':('Свободный дальний торец хребта' if site.kind=='end' else 'Тройник существующего хребта'),'target':(port_u,v),
                                      'score':round(cost,5),'length':round(distance,5),
                                      'bends':turns,'result':'допустим'})
            # Для транзита сравниваем ЛУЧШИЙ разрешённый боковой вход
            # с ЛУЧШИМ присоединением к хребту, а не выбираем всегда хребет.
            all_options=[(row[0],1,row) for row in candidates]
            all_options.extend((row[0],0,row) for row in side_candidates)
            if all_options:
                _,mode,chosen=min(all_options,key=lambda row:(row[0],row[1],row[2][1]))
                _,_,turns,_,port_u,trail=chosen
                record_choice('Боковая грань плиты' if mode==0 else
                              'Свободный дальний торец хребта' if abs(port_u-reach)<1e-6 else
                              'Тройник хребта на плите', chosen[0],trail)
                phase=line_phase(wp,m)
                if mode==0:
                    purpose=f'Транзитная линия скв. {wp.well.number}: боковой порт центральной плиты'
                    for aa,bb in zip(trail,trail[1:]):add(out,aa,bb,m,phase,purpose)
                else:
                    purpose=(f'К дальнему торцу хребта от скв. {wp.well.number}' if
                             abs(port_u-reach)<1e-6 else
                             f'К существующему хребту от скв. {wp.well.number}')
                    add(out,outlet,lead,m,phase,purpose)
                    for aa,bb in zip(trail,trail[1:]):add(out,aa,bb,m,phase,purpose)
                bends+=turns
                ports[m]=port_u
                continue
        # Если хребта своей среды нет, всё равно пробуем боковой порт,
        # прежде чем направлять длинную трассу к торцу плиты.
        if side_candidates:
            best_side=min(side_candidates)
            direct_distance=abs(lead.u-head_u)+abs(lead.v-v)
            # Для СППВ, ППД и продуктовой нефти торец плиты не является резервным
            # вариантом. Боковой подход обязателен, если не выбран хребет.
            if m in SIDE_ONLY_PLATE_MEDIA or (riser_u is None and best_side[0] < direct_distance + bend_penalty*2):
                _,_,turns,_,port_u,trail=best_side
                record_choice('Боковая грань плиты',best_side[0],trail)
                purpose=f'Транзитная линия скв. {wp.well.number}: боковой порт центральной плиты'
                phase=line_phase(wp,m)
                # Не теряем первые 2 м от скважины до точки lead.
                for aa,bb in zip(trail,trail[1:]):add(out,aa,bb,m,phase,purpose)
                bends+=turns
                ports[m]=port_u
                continue
        if m in SIDE_ONLY_PLATE_MEDIA:
            record_choice('Нет допустимого маршрута')
            raise ValueError(f'Скважина №{wp.well.number}: нет допустимого бокового подключения '
                             f'{MEDIA_RU[m]} к центральной плите или тройника на хребте. '
                             'Торцевой подвод этой среды запрещён.')
        endpoint=Pt(head_u,v)
        path=[lead]
        stages=[]
        if riser_u is not None:
            corridor_u=riser_u
            corridor_shift=line_offsets.get((wp.well.number,m),0.0)
            corridor_v=(staging_v + corridor_shift) if staging_v is not None else (lead.v + corridor_shift)
            if abs(corridor_v-lead.v)>EPS:
                stages.append(Pt(lead.u,corridor_v))
            if abs(corridor_u-lead.u)>EPS:
                stages.append(Pt(corridor_u,corridor_v))
        # Последний участок внеосевой трубы приходит в порт центра
        # ПОПЕРЁК его створа, а не совпадает с вертикальной нефтяной
        # магистралью, выходящей из центра к точке врезки.
        if abs(endpoint.u-path[-1].u)>EPS or abs(endpoint.v-path[-1].v)>EPS:
            sign_to_approach=(1 if path[-1].u>=head_u else -1)
            inlet_shift=1.5+abs(line_offsets.get((wp.well.number,m),0.0))
            inlet=Pt(head_u+sign_to_approach*inlet_shift,endpoint.v)
            if abs(inlet.u-endpoint.u)>EPS:
                stages.append(inlet)
        for waypoint in stages+[endpoint]:
            trail=rectilinear_safe_path(path[-1],waypoint,all_wells,
                        owner=wp.well.number,bend_penalty=bend_penalty,
                        existing=tuple(existing)+tuple(out),medium=m,
                        inner_zone=inner_zone)
            path.extend(trail[1:])
        purpose=(f'Общий коридор к центральному узлу от скв. {wp.well.number}'
                 if riser_u is not None else f'К центральному узлу от скв. {wp.well.number}')
        phase=line_phase(wp,m)
        add(out,outlet,lead,m,phase,purpose)
        for a,b in zip(path,path[1:]):add(out,a,b,m,phase,purpose)
        nodes=[outlet]+path
        bends+=sum(1 for a,b,c in zip(nodes,nodes[1:],nodes[2:])
                    if (abs(a.u-b.u)<EPS)!=(abs(b.u-c.u)<EPS))
        ports[m]=head_u
        record_choice('Запасной маршрут к центральному узлу',None,[outlet]+path)
    return out,ports,bends

def route_group(wells,lanes,u_center=None,bundle_gap=.5,head_u=-1e12):
    media={m for wp in wells for m,_ in wp.connections if m!='oil_measure'}
    offsets=bundle_offsets(media,bundle_gap)
    out=[];ports={};bends=0
    for wp in wells:
        seg,pp,n=route_of_well(wp,lanes,u_center,offsets,None,head_u)
        out.extend(seg)
        ports.update({(wp.well.number,m):u for m,u in pp.items()})
        bends+=n
    return out,ports,bends



def medium_flow_sources(placements,medium):
    """Источники потока по направлению движения среды, а не потребители.

    При отсутствии указанных в проекте источников статус сети не выводится
    из предположений о внешнем источнике (например, на другой площадке).
    """
    source_kind={'fresh':'fresh_source','pressure':'ppd_source',
                 'oil_product':'producer','oil_measure':'producer','gas':'producer'}[medium]
    return [wp for wp in placements
            if wp.well.kind==source_kind and wp.well.status!='inactive'
            and (medium!='gas' or wp.well.gas)
            and (medium=='oil_measure' or any(m==medium for m,_ in wp.connections))]


def all_flow_sources_future(placements,medium):
    sources=medium_flow_sources(placements,medium)
    return bool(sources) and all(line_phase(wp,medium)=='future' for wp in sources)


def propagate_future_flow(segs,placements):
    """Если ВСЕ источники потока среды перспективные — ВСЯ сеть этой среды перспективна."""
    future_media={m for m in MEDIA if all_flow_sources_future(placements,m)}
    return ([Segment(s.a,s.b,s.medium,'future' if s.medium in future_media else s.phase,s.purpose)
             for s in segs],future_media)


def build_tie_corridor_overrides(p,baseline,wp_list, *, media_order=None, axis_sign=None):
    """Одна транзитная нитка на СРЕДУ с несколькими отводами/тройниками.

    До гидравлического расчёта объединение допустимо лишь как предварительная
    топология — диаметры и направления потока требуется уточнить инженеру.
    """
    if not wp_list:return {}
    oil_segments=[s for s in baseline.segments if s.medium=='oil_product' and
                  'от центра к врезке' in s.purpose.lower() and s.length>EPS]
    if not oil_segments:return {}
    transverse=[s for s in oil_segments if not s.horizontal and s.length>2.0]
    spine=max(transverse or oil_segments,key=lambda s:s.length)
    direction='v' if not spine.horizontal else 'u'
    oil_axis=spine.a.u if direction=='v' else spine.a.v
    sample=sum((wp.point.u if direction=='v' else wp.point.v) for wp in wp_list)/len(wp_list)
    sign=(1 if sample>=oil_axis else -1) if axis_sign is None else axis_sign
    media=sorted({m for wp in wp_list for m,_ in wp.connections if m!='oil_measure'},key=MEDIA.index)
    if not media:return {}
    # Порядок ниток НЕ задан типом среды: перебираем положения на обеих
    # сторонах продуктовой трубы. Иначе ветка одной скважины может
    # пересечь чужую общую линию и сделать хороший коридор «дорогим».
    if media_order is not None:
        if set(media_order)!=set(media):raise ValueError('Неверный набор сред в коридоре')
        media=list(media_order)
    # sign может быть общим для всего пучка или словарём сторон
    # отдельных ниток. Вариант с продуктовой трубой ПОСЕРЕДИНЕ
    # позволяет избежать лишних пересечений подводов к ней.
    signs={m:(axis_sign.get(m,sign) if isinstance(axis_sign,dict) else sign) for m in media}
    if any(direction not in (-1,1) for direction in signs.values()):
        raise ValueError('Направление каждой нитки должно быть -1 или 1')
    last_by_sign={-1:'oil_product',1:'oil_product'}
    dist_by_sign={-1:0.,1:0.}
    axes={}
    for m in media:
        offset_side=signs[m]
        dist_by_sign[offset_side]+=p.lane_gap+(PIPE_OD[last_by_sign[offset_side]]+PIPE_OD[m])/2
        axes[m]=oil_axis+offset_side*dist_by_sign[offset_side]
        last_by_sign[offset_side]=m
    return {wp.well.number:{'tie_corridor':direction,'media_axes':axes}
            for wp in wp_list}


def route_well_via_tie(wp,lanes,opts,head_u,side,gap,all_wells,bend_penalty,existing,taps=None,planned_shared_joins=None,central_plate=None):
    """От скважины только до тройника на общей нитке, а НЕ отдельная труба до центра.

    Shared backbone is authored ONCE per medium by build().
    """
    lines=[];ports={};bends=0;joins={}
    media=[m for m,_ in wp.connections if m!='oil_measure']
    direction=opts['tie_corridor']
    taps=taps or {}
    ref_axis=next(iter(opts['media_axes'].values()))
    inner_zone=trunk_inner_zone(lanes,side,head_u,all_wells)
    # Направляем выход трубы к своему хребту, а не по знаку side:
    # при параллельной ориентации скважины старая подсказка имела ту же
    # V-координату, что и сама скважина, и вывод разворачивался ОТ узла.
    corridor_target=(Pt(ref_axis,lanes[media[0]]) if direction=='v'
                     else Pt(head_u,ref_axis))
    for medium in media:
        tap=taps.get(medium)
        if tap is not None:
            donor_number,junction=tap
            donor=next((w for w in all_wells if w.well.number==donor_number),None)
            if donor is None or donor.well.kind!=wp.well.kind or (line_phase(wp,medium)=='active'
                                 and line_phase(donor,medium)=='future'):
                raise ValueError('Действующая линия не может питаться от перспективной')
            # Действующая труба и её продолжение после отвода — единый
            # маршрут, даже когда после поворота меняется ``purpose``.
            # В момент построения донорская общая нитка ещё не добавлена
            # к сегментам: добавляем её ожидаемый участок для проверки,
            # не меняя геометрию результирующей сети.
            from continuous_routes import ContinuousNetwork
            physical=list(existing)
            if planned_shared_joins:
                # Временный граф повторяет настоящее построение общей нитки
                # ВКЛЮЧАЯ два поворота и боковой вход в центральную плиту.
                # Куски для проверки НЕ добавляются в результат.
                add_shared_tie_backbones(physical,planned_shared_joins,lanes,
                                          head_u,all_wells,central_plate=central_plate)
            donor_segments=[s for s in existing if s.medium==medium and
                 re.search(r'скв\.\s*'+str(donor_number)+r'(?!\d)',s.purpose,re.IGNORECASE)]
            if not donor_segments:
                raise ValueError(f'Не найден подключённый маршрут скважины №{donor_number}')
            start=min((pt for s in donor_segments for pt in (s.a,s.b)),
                      key=lambda q:abs(q.u-donor.point.u)+abs(q.v-donor.point.v))
            network=ContinuousNetwork(physical,medium,
                active_only=(line_phase(wp,medium)=='active'),extra_points=[junction,start])
            if not network.connected(start,junction):
                raise ValueError(f'Нельзя подключить скв. {wp.well.number}: '
                                 'выбранная точка не лежит на непрерывном маршруте '
                                 f'скважины №{donor_number} той же среды')
        else:
            axis=opts['media_axes'][medium]
            junction=None
        # При присоединении к существующей трубе патрубок ориентируется
        # на реальную точку, а для обычного отвода сохранено положение v20.
        face_target=(junction if tap is not None else
                     (Pt(opts['media_axes'][medium],lanes[medium]) if direction=='v'
                      else Pt(head_u,opts['media_axes'][medium])))
        origin_medium=well_medium_outlets(wp,side,face_target,gap)
        outlet=origin_medium[medium]
        normal_medium=well_connection_normal(wp,side,face_target)
        initial_straight=WELL_MIN_STRAIGHT if wp.well.on_axis else OFFAXIS_MIN_STRAIGHT
        lead=Pt(outlet.u+normal_medium.u*initial_straight,
                outlet.v+normal_medium.v*initial_straight)
        if tap is None:
            junction=(Pt(axis,lead.v) if direction=='v' else Pt(lead.u,axis))
        nodes=[outlet,lead]
        if abs(junction.u-lead.u)>EPS or abs(junction.v-lead.v)>EPS:
            trail=rectilinear_safe_path(lead,junction,all_wells,owner=wp.well.number,
                   bend_penalty=bend_penalty,existing=tuple(existing)+tuple(lines),medium=medium,
                   inner_zone=inner_zone)
            if len(trail)>1:
                first=trail[1]
                dot=(first.u-lead.u)*normal_medium.u+(first.v-lead.v)*normal_medium.v
                if dot< -EPS:
                    raise ValueError('Маршрут разворачивается сразу после выхода из скважины')
            nodes.extend(trail[1:])
        phase=line_phase(wp,medium)
        purpose=(f'Отвод скв. {wp.well.number} к существующей нитке {MEDIA_RU[medium]}'
                 if tap is not None else
                 f'Отвод скв. {wp.well.number} к общей нитке {MEDIA_RU[medium]}')
        for a,b in zip(nodes,nodes[1:]):add(lines,a,b,medium,phase,purpose)
        bends+=sum(1 for a,b,c in zip(nodes,nodes[1:],nodes[2:])
                   if (abs(a.u-b.u)<EPS)!=(abs(b.u-c.u)<EPS))
        if tap is None:
            joins[medium]=junction
        ports[medium]=head_u
    return lines,ports,bends,joins


def add_shared_tie_backbones(segs,joins,lanes,head_u,placements,central_plate=None):
    """Разместить общую магистраль один раз на каждую среду и соединить с портом центра.

    Концы отводов становятся точками присоединения. Длины и масса общих труб
    учитываются ровно один раз, независимо от числа подключённых скважин.
    """
    bends=0
    remote_bundled=[]
    if central_plate is not None:
        for med,rows in joins.items():
            if rows and rows[0][2]=='v' and rows[0][0].u < central_plate.u_min-0.05:
                remote_bundled.append((abs(rows[0][0].u-central_plate.center_u),med))
    remote_bundled.sort()
    entry_rank={med:i for i,(_,med) in enumerate(remote_bundled)}
    for medium,items in joins.items():
        direction=items[0][2]
        axes={round(point.u if direction=='v' else point.v,6) for point,_,_,_ in items}
        if len(axes)!=1:
            raise ValueError(f'Нарушена общая ось нитки {MEDIA_RU[medium]}')
        axis=float(next(iter(axes)))
        # Общий вертикальный коридор допустим у бокового порта (ось U
        # ВНУТРИ плиты) или дальше по хребту. Протяжку через наружный
        # торец плиты запрещаем. Горизонтальную общую линию, приходящую
        # в торец поперёк оси магистрали, тоже исключаем.
        side_connection=(
            central_plate is not None and direction=='v' and
            central_plate.u_min+0.35 < axis < central_plate.u_max-0.35)
        # Вертикальный коридор за наружным торцом: подходим к длинной
        # стороне плиты через отдельный боковой ввод, НЕ через торец.
        remote_side_connection=(central_plate is not None and direction=='v'
             and axis < central_plate.u_min-0.05)
        if medium in SIDE_ONLY_PLATE_MEDIA and (
            direction != 'v' or (axis <= head_u+1e-5 and not side_connection
                                and not remote_side_connection)
        ):
            raise ValueError(f'Общая нитка {MEDIA_RU[medium]}: торцевое подключение '
                             'к центральной плите запрещено, выберите боковой порт или хребет.')
        endpoint=Pt(head_u,lanes[medium])
        # All shared trunk joins obey the SAME supported-grid rule as an
        # independent well. A group cannot bypass it via a synthetic trunk.
        if direction=='v' and not side_connection and not remote_side_connection:
            from product_tie_rules import projected_trunk_sites
            axial=[wp for wp in placements if wp.well.on_axis]
            if not axial or not any(m==medium for wp in axial for m,_ in wp.connections):
                raise ValueError('Общий коридор не может присоединиться к отсутствующему хребту своей среды')
            far=max(wp.point.u for wp in axial)-head_u
            allowed=projected_trunk_sites(placements,central_plate,lanes,
                         medium,head_u,far,'active' if any(ph=='active' for _,ph,_,_ in items) else 'future')
            if not any(abs(q.point.u-axis)<1e-5 for q in allowed):
                raise ValueError('Общий коридор: точка хребта должна находиться на опорной плите (шаг 0,1 м) либо на дальнем торце')
        if direction=='v' and (side_connection or remote_side_connection):
            from product_tie_rules import tick_values, END_MARGIN
            valid_u=tick_values(central_plate.u_min+END_MARGIN,
                                central_plate.u_max-END_MARGIN)
            if side_connection and not any(abs(axis-u)<1e-5 for u in valid_u):
                raise ValueError('Общий боковой порт центральной плиты должен быть на сетке 0,1 м')
            expected=1 if items[0][0].v>=central_plate.center_v else -1
            if any((1 if q.v>=central_plate.center_v else -1)!=expected
                   for q,_,_,_ in items):
                raise ValueError('Объединяемые скважины находятся по разные стороны центральной плиты')
        # Общее подключение к центральному узлу — одна труба на среду.
        phase='active' if any(ph=='active' for _,ph,_,_ in items) else 'future'
        owners=' '.join(f'скв. {n}' for n in sorted({n for *_,n in items}))
        purpose=f'Общая нитка {MEDIA_RU[medium]} ({owners})'
        if direction=='v':
            # Разветвления могут быть с обеих сторон общей вертикали.
            mid=Pt(axis,endpoint.v)
            if remote_side_connection:
                v_mean=sum(q.v for q,_,_,_ in items)/len(items)
                rank=entry_rank.get(medium,0)
                # Упорядоченные боковые подводы: ближняя к продуктовой
                # труба выходит чуть дальше от плиты, а следующая — ближе.
                # Иначе два подводящих участка ставили концы в одной точке
                # либо пересекались ещё до входа на плиту.
                stagger=0.5*(len(remote_bundled)-1-rank)
                v_side=(central_plate.v_max+0.55+stagger if v_mean>=central_plate.center_v
                        else central_plate.v_min-0.55-stagger)
                u_side=max(central_plate.u_min+0.35,min(central_plate.u_max-0.35,
                           central_plate.center_u-0.45*rank))
                # От общей вертикали через наружный коридор к боковому
                # порту своей среды; поперечные пересечения при этом
                # записываются на ПОДВОДИМОЙ трубе, не на плите.
                add(segs,Pt(axis,v_side),Pt(u_side,v_side),medium,phase,purpose)
                add(segs,Pt(u_side,v_side),Pt(u_side,endpoint.v),medium,phase,purpose)
                for v in sorted({q.v for q,_,_,_ in items}):
                    add(segs,Pt(axis,v),Pt(axis,v_side),medium,phase,purpose)
                bends+=2
            else:
                if not side_connection:
                    # Общая вертикаль действительно приходит в хребет.
                    # Если её питают точки со стороны НДС, такая группа
                    # недопустима; вместо нарушения оптимизатор выбирает
                    # независимые боковые маршруты или другую группу.
                    sign_side=1 if lanes['oil_product']>0 else -1
                    well_by_number={w.well.number:w for w in placements}
                    if any((well_by_number[n].point.v-endpoint.v)*sign_side<=1e-5
                           for _,_,_,n in items if n in well_by_number):
                        raise ValueError('Общий коридор подходит к хребту с внутренней стороны НДС')
                    continuation=('Хребет' if phase=='active' else 'Перспективный хребет') if medium in SIDE_ONLY_PLATE_MEDIA and axis>head_u+1e-5 else purpose
                    add(segs,mid,endpoint,medium,phase,continuation)
                    if abs(axis-endpoint.u)>EPS and any(abs(q.v-endpoint.v)>EPS for q,_,_,_ in items):bends+=1
                for v in sorted({q.v for q,_,_,_ in items}):
                    add(segs,mid,Pt(axis,v),medium,phase,purpose)
        else:
            mid=Pt(endpoint.u,axis)
            add(segs,mid,endpoint,medium,phase,purpose)
            if abs(axis-endpoint.v)>EPS and any(abs(q.u-endpoint.u)>EPS for q,_,_,_ in items):bends+=1
            for u in sorted({q.u for q,_,_,_ in items}):
                add(segs,mid,Pt(u,axis),medium,phase,purpose)
    return bends


def _access_to_trunk_distances(result:Result)->dict[tuple[int,str],float]:
    """Кратчайший путь ПО ТРУБЕ от внеосевой скважины до хребта своей среды.

    Учитывает общую нитку и тройники: просто суммировать сегменты с именем
    скважины неверно, так как у нескольких скважин бывают общие участки.
    """
    from pipe_render import make_network
    network=make_network(result.segments)
    graph={}
    for medium,edges in network.items():
        adjacency={}
        for a,b,_ in edges:
            weight=abs(a[0]-b[0])+abs(a[1]-b[1])
            adjacency.setdefault(a,[]).append((b,weight))
            adjacency.setdefault(b,[]).append((a,weight))
        graph[medium]=adjacency
    trunk={m:[s for s in result.segments if s.medium==m and
               ('Хребет' in s.purpose or 'хребта' in s.purpose or
                'Перспективное продление хребта' in s.purpose)] for m in MEDIA}
    targets={m:{q for q in graph.get(m,{}) if any(
                 distance_segment_to_point(s.a,s.b,Pt(*q))<1e-5
                 for s in trunk[m])} for m in MEDIA}
    costs={}
    for wp in result.wells:
        if wp.well.on_axis or wp.well.status=='inactive':continue
        outlets=well_medium_outlets(wp,result.trunk_side,result.center,
                                    result.project.lane_gap)
        for medium,outlet in outlets.items():
            source=(round(outlet.u,6),round(outlet.v,6))
            if source not in graph.get(medium,{}):
                costs[(wp.well.number,medium)]=math.inf
                continue
            todo=[(0.,source)];dist={source:0.}
            distance=math.inf
            while todo:
                current,node=heapq.heappop(todo)
                if current>dist[node]+EPS:continue
                if node in targets[medium]:
                    distance=current;break
                for neighbour,weight in graph[medium].get(node,()):
                    alt=current+weight
                    if alt<dist.get(neighbour,math.inf)-EPS:
                        dist[neighbour]=alt
                        heapq.heappush(todo,(alt,neighbour))
            costs[(wp.well.number,medium)]=distance
    return costs


def excessive_remote_detour(baseline:Result,trial:Result)->bool:
    """Не заменяем короткое присоединение к хребту длинным коридором.

    Допустимые общие коридоры остаются разрешены, но маршрут каждой
    внеосевой скважины с уже доступным прямым присоединением не должен
    становиться на десятки метров длиннее ради малой общей экономии.
    """
    direct={(wp.well.number,med) for wp in baseline.wells if not wp.well.on_axis
            for med,_ in wp.connections
            if any(s.medium==med and
                   s.purpose==f'К существующему хребту от скв. {wp.well.number}'
                   for s in baseline.segments)}
    if not direct:return False
    initial=_access_to_trunk_distances(baseline)
    revised=_access_to_trunk_distances(trial)
    for key in direct:
        a=initial.get(key,math.inf);b=revised.get(key,math.inf)
        if math.isfinite(a) and (not math.isfinite(b) or
                                  b>a+max(18.,.45*a)+1e-5):
            return True
    return False


def optimize_tie_corridor(p,baseline,side,orientations):
    """Полная оценка набора компактных пучков рядом с продуктовой врезкой.

    Пробуем обе стороны продуктовой нитки и все порядки сред (до трёх
    различных). Проверяем индивидуальные и групповые варианты. Никакой
    кандидат не принимается, если ухудшает J либо создаёт новую коллизию.
    Пожарный вывод не является направляющей транзитного коридора.
    """
    if not p.shared_corridors:return baseline
    remotes=[w for w in baseline.wells if not w.well.on_axis and w.well.status!='inactive' and w.connections]
    if not remotes:return baseline
    oil_routes=[s for s in baseline.segments if s.medium=='oil_product' and 'от центра к врезке' in s.purpose.lower()]
    if not oil_routes:return baseline
    transverse=[s for s in oil_routes if not s.horizontal and s.length>2.0]
    spine=max(transverse or oil_routes,key=lambda s:s.length,default=None)
    if spine is None:return baseline
    coord=spine.a.u if not spine.horizontal else spine.a.v
    orient='v' if not spine.horizontal else 'u'
    near=[w for w in remotes if abs((w.point.u if orient=='v' else w.point.v)-coord)<=125]
    if not near:return baseline
    near=sorted(near,key=lambda w:(abs((w.point.u if orient=='v' else w.point.v)-coord),w.point.u,w.point.v))[:3]
    # Сначала рассматриваем пары, затем тройку и одиночные нитки:
    # так ограниченный поиск не расходует весь бюджет на одиночные варианты.
    all_sets=[]
    if len(near)>1:
        all_sets += [[a,b] for ix,a in enumerate(near) for b in near[ix+1:]]
    if len(near)==3:all_sets.append(near)
    all_sets += [[w] for w in near]
    # Каждый вызов build() проверяет полную геометрию, а не только пару линий.
    max_trials = 12 if len(remotes)>2 else 64
    best=baseline
    bundle_attempts=0
    bundle_valid=0
    rejection_stats={}
    attempted_groups=set()
    def reject_bundle(reason):
        rejection_stats[reason]=rejection_stats.get(reason,0)+1
    orientation_base=dict(orientations or {w.number:w.orientation for w in baseline.project.wells})
    for group in all_sets:
        if bundle_attempts>=max_trials:break
        used=sorted({m for wp in group for m,_ in wp.connections if m!='oil_measure'},key=MEDIA.index)
        if not used or len(used)>3:continue
        if len(group)==1 and abs((group[0].point.u if orient=='v' else group[0].point.v)-coord)>60:
            continue
        group_names=tuple(sorted(w.well.number for w in group))
        seen_axes=set()
        for ordering in permutations(used):
            if bundle_attempts>=max_trials:break
            for signs in product((-1,1),repeat=len(used)):
                if bundle_attempts>=max_trials:break
                sign_map=dict(zip(ordering,signs))
                overrides=build_tie_corridor_overrides(p,baseline,group,
                                      media_order=ordering,axis_sign=sign_map)
                if not overrides:continue
                axes=overrides[group[0].well.number]['media_axes']
                signature=tuple((m,round(axes[m],5)) for m in sorted(axes))
                if signature in seen_axes:continue
                seen_axes.add(signature)
                bundle_attempts+=1
                attempted_groups.add(group_names)
                try:
                    trial=build(p,_overrides=overrides,
                                _bundles=[group_names] if len(group)>1 else [],
                                _side=side,_orientations=orientation_base)
                except (ValueError,AssertionError) as exc:
                    reject_bundle('Недопустимая геометрия: '+str(exc)[:90])
                    continue
                bundle_valid+=1
                if (trial.metrics.get('foreign_contacts',0)>best.metrics.get('foreign_contacts',0) or
                    trial.metrics.get('overlaps',0)>best.metrics.get('overlaps',0) or
                    excessive_remote_detour(baseline,trial)):
                    reject_bundle('Запрещённые контакты/наложения или чрезмерный обход')
                    continue
                if score(trial)>score(best)+1e-6:
                    reject_bundle('Допустимо, но полная стоимость J выше')
                    continue
                # Равная стоимость не повод перестраивать независимые линии в пучок.
                if score(trial)>=score(best)-1e-6:
                    continue
                best=trial
                best.notes.append('Пучок у продуктовой линии: '+', '.join(
                    f'{MEDIA_RU[m]}' for m in ordering)+
                    f'; схема сторон: {", ".join(f"{MEDIA_RU[m]}={sign_map[m]:+d}" for m in ordering)}. '
                    'Проверены альтернативные порядок и сторона прокладки.')
    best.decision_trace['_bundle_search']={
        'nearby_wells':[w.well.number for w in near],
        'attempted_groups':[list(group) for group in sorted(attempted_groups)],
        'evaluated':bundle_attempts,
        'trial_budget':max_trials,
        'geometrically_valid':bundle_valid,
        'rejections':dict(sorted(rejection_stats.items(),key=lambda x:-x[1])[:8]),
        'chosen_groups':[list(g) for g in best.routing_groups],
        'note':'Это проверенные варианты вокруг выбранной продуктовой трассы, а не полный перебор всех коридоров.'
    }
    return best


def attachment_points(seg:Segment, well_pos:Pt)->list[Pt]:
    """Точки кандидатов на уже существующем отводе: проекция, концы,
    и промежуточные точки, если проекция близка к занятой зоне.
    """
    if seg.horizontal:
        lo,hi=sorted((seg.a.u,seg.b.u))
        values=[lo,hi,min(max(well_pos.u,lo),hi),(lo+hi)/2,
                lo+(hi-lo)/3,lo+2*(hi-lo)/3]
        return [Pt(round(u,6),seg.a.v) for u in values]
    lo,hi=sorted((seg.a.v,seg.b.v))
    values=[lo,hi,min(max(well_pos.v,lo),hi),(lo+hi)/2,
            lo+(hi-lo)/3,lo+2*(hi-lo)/3]
    return [Pt(seg.a.u,round(v,6)) for v in values]


def optimize_same_medium_taps(p:Project, initial:Result, side:int)->Result:
    """Локальный поиск присоединений удалённых скважин к ответвлениям
    уже подключённых скважин ТОЙ ЖЕ среды, а не только к общей нитке.

    Это сокращает дублированные подходы к центральному узлу. Для каждого
    кандидата полностью перестраивается сеть, общая магистраль обрезается
    по оставшимся присоединениям и заново считаются коллизии и метраж.
    Не подключаемся к будущему отрезку, если скважина обустраивается сейчас.
    """
    if not initial.routing_groups:
        return initial
    base_orients={wp.well.number:wp.orientation for wp in initial.wells}
    group_names={n for g in initial.routing_groups for n in g}
    group=[wp for wp in initial.wells if wp.well.number in group_names]
    overrides=build_tie_corridor_overrides(p,initial,group)
    if not overrides:
        return initial
    bundles=initial.routing_groups
    best=initial
    selected={}
    max_evaluations=36
    evaluations=0
    for target in sorted(group,key=lambda wp:wp.well.number,reverse=True):
        if target.well.status=='inactive':continue
        media=[m for m,_ in target.connections if m!='oil_measure']
        if not media:
            continue
        for medium in media:
            if evaluations>=max_evaluations:break
            donors=[wp for wp in group if wp.well.number<target.well.number
                    and wp.well.number not in {n for n,_ in selected}
                    and wp.well.kind==target.well.kind
                    and wp.well.status==target.well.status
                    and any(m==medium for m,_ in wp.connections)
                    and not (line_phase(target,medium)=='active' and line_phase(wp,medium)=='future')]
            if not donors:continue
            rough=[]
            # Не ограничиваем маршрут назначением единственного прямого куска
            # до отвода. Граф содержит весь соединённый транзит данной среды:
            # повороты, общие магистрали и тройники.
            from continuous_routes import ContinuousNetwork
            network=ContinuousNetwork(best.segments,medium,
                                      active_only=(line_phase(target,medium)=='active'))
            connected_candidates=[]
            for donor in donors:
                donor_segments=[seg for seg in best.segments
                   if seg.medium==medium and
                   re.search(r'скв\.\s*'+str(donor.well.number)+r'(?!\d)',seg.purpose,re.IGNORECASE)]
                if not donor_segments:
                    continue
                source=min((q for seg in donor_segments for q in (seg.a,seg.b)),
                           key=lambda q:abs(q.u-donor.point.u)+abs(q.v-donor.point.v))
                component=network.component(source)
                if not component:
                    continue
                for seg in best.segments:
                    if seg.medium!=medium:
                        continue
                    if line_phase(target,medium)=='active' and seg.phase!='active':
                        continue
                    if seg.purpose in ('Хребет','Перспективный хребет',
                                       'Перспективное продление хребта',PLATE_PURPOSE):
                        continue  # Для хребта есть отдельный алгоритм тройников.
                    if 'пожарного узла' in seg.purpose.casefold():
                        continue  # Независимая линия пожарного узла недоступна.
                    if seg.purpose.startswith('Отвод от скв. '):
                        continue  # Осевые отпайки не считаем транзитными.
                    if re.search(r'скв\.\s*'+str(target.well.number)+r'(?!\d)',
                                 seg.purpose,re.IGNORECASE) and 'Общая нитка' not in seg.purpose:
                        continue
                    if ((round(seg.a.u,6),round(seg.a.v,6)) not in component or
                        (round(seg.b.u,6),round(seg.b.v,6)) not in component):
                        continue
                    connected_candidates.append((donor.well.number,seg))
            for donor_number,seg in connected_candidates:
                for anchor in attachment_points(seg,target.point):
                    if not protected_line_ok(anchor,anchor,best.wells,owner=target.well.number):
                        continue
                    estimate=abs(anchor.u-target.point.u)+abs(anchor.v-target.point.v)
                    # Предварительная оценка включает возможность дополнительного
                    # поворота; итоговое решение по-прежнему принимает полная J.
                    rough.append((estimate,donor_number,anchor))
            seen=set()
            rough.sort(key=lambda x:(x[0],x[1],x[2].u,x[2].v))
            candidate_list=[]
            for estimate,donor,anchor in rough:
                key=(donor,round(anchor.u,6),round(anchor.v,6))
                if key in seen:continue
                seen.add(key)
                candidate_list.append((donor,anchor))
                if len(candidate_list)>=12:break
            new_best=None
            current_score=score(best)
            for donor,anchor in candidate_list:
                if evaluations>=max_evaluations:break
                evaluations+=1
                trial_taps={**selected,(target.well.number,medium):(donor,anchor)}
                try:
                    trial=build(p,_overrides=overrides,_bundles=bundles,_side=side,
                                _orientations=base_orients,_taps=trial_taps)
                except (ValueError,AssertionError):
                    continue
                if trial.metrics['overlaps']>best.metrics['overlaps'] or \
                   trial.metrics.get('foreign_contacts',0)>best.metrics.get('foreign_contacts',0):
                    continue
                # Optimization is meaningful only when the whole network, rather
                # than an individual line on the display, actually improves.
                current=score(trial)
                if current < current_score-1e-5:
                    new_best=(trial,trial_taps,donor,anchor,current)
                    current_score=current
            if new_best is not None:
                best,selected,donor,anchor,_=new_best
                best.notes.append(
                    f'Скважина №{target.well.number}: {MEDIA_RU[medium]} подключена '
                    f'к существующей линии скважины №{donor} в точке '
                    f'U={anchor.u:.2f}; V={anchor.v:.2f} м. '
                    'Метраж и общая нитка пересчитаны.')
            if evaluations>=max_evaluations:break
        if evaluations>=max_evaluations:break
    return best


def score(result:Result):
    """Единая настраиваемая условная стоимость всех допустимых кандидатов."""
    from network_cost import estimate_score
    return estimate_score(result)


def optimize_groups(p:Project,baseline:Result,side:int=1,orientations=None):
    """Глобальная оценка сетки на каждом шаге, перебор соседних групп и точек подключения.
    Не произвольный непрерывный глобальный оптимум: конечное множество допустимых
    групп и кандидатов, ограниченных 45 метрами и 6 скважинами.
    Гарантия: не выбирать конфигурацию с более высокой функцией затрат, чем исходная.
    """
    remote=[]
    lo=min(baseline.lanes.values());hi=max(baseline.lanes.values())
    for wp in baseline.wells:
        if wp.well.on_axis or wp.well.status=='inactive' or not wp.connections:
            continue
        if wp.point.v>hi+2: zone='upper'
        elif wp.point.v<lo-2:zone='lower'
        else:continue
        remote.append((wp,(zone,wp.well.status)))
    if len(remote)<2:return baseline
    if len(remote)>3:
        baseline.notes.append('Более трёх внеосевых скважин: сравниваются локальные соседние пары по зоне и статусу. '
                              'Полный перебор всех возможных комбинаций не выполняется.')
    groups=[{'wells':[wp],'x':None,'key':key} for wp,key in remote]
    best_result=baseline
    for _ in range(min(1 if len(remote)>3 else 4,len(groups)-1)):
        ordered=sorted(enumerate(groups),key=lambda k:sum(w.point.u for w in k[1]['wells'])/len(k[1]['wells']))
        best_trial=None;best_cost=score(best_result)
        # Для больших кустов ограничиваем КОЛИЧЕСТВО полностью пересчитываемых
        # близких пар, но никогда не отключаем оптимизацию целиком.
        neighbors=list(zip(ordered,ordered[1:]))
        if len(remote)>3:
            neighbors.sort(key=lambda pair:abs(
                sum(w.point.u for w in pair[0][1]['wells'])/len(pair[0][1]['wells'])-
                sum(w.point.u for w in pair[1][1]['wells'])/len(pair[1][1]['wells'])))
            neighbors=neighbors[:2]
        for (ia,ga),(ib,gb) in neighbors:
            if ga['key']!=gb['key']:continue
            ws=ga['wells']+gb['wells']
            us=[w.point.u for w in ws]
            if len(ws)>6 or max(us)-min(us)>45:continue
            su=sorted(us)
            candidates=sorted(set(round(x,5) for x in
                ([(su[0]+su[-1])/2] if len(remote)>3
                 else [su[0],su[-1],(su[0]+su[-1])/2])))
            for x in candidates:
                proposed=[g for z,g in enumerate(groups) if z not in (ia,ib)]
                proposed.append({'wells':ws,'x':x,'key':ga['key']})
                overrides={};bundles=[]
                for grp in proposed:
                    if grp['x'] is None or len(grp['wells'])<2:continue
                    media_set={m for wp in grp['wells'] for m,_ in wp.connections if m!='oil_measure'}
                    offsets=bundle_offsets(media_set,p.lane_gap)
                    line_keys=sorted([(wp.well.number,m) for wp in grp['wells'] for m,_ in wp.connections if m!='oil_measure'], key=lambda k:(MEDIA.index(k[1]),k[0]))
                    line_offsets=bundle_offsets_for_lines(line_keys,p.lane_gap)
                    rim=(WELL_GUARD_RADIUS+1.0)
                    staging_v=(min(w.point.v for w in grp['wells'])-rim if grp['key'][0]=='upper'
                               else max(w.point.v for w in grp['wells'])+rim)
                    for wp in grp['wells']:
                        overrides[wp.well.number]={'center':grp['x'],'offsets':offsets,'line_offsets':line_offsets,'staging_v':staging_v}
                    bundles.append(tuple(sorted(w.well.number for w in grp['wells'])))
                try:
                    trial=build(p,_overrides=overrides,_bundles=bundles,_side=side,_orientations=orientations)
                except ValueError:
                    continue  # Запретная зона: кандидат недопустим, даже если короткий.
                if trial.metrics.get('foreign_contacts',0)>best_result.metrics.get('foreign_contacts',0):
                    continue
                if excessive_remote_detour(best_result,trial):
                    continue
                candidate_score=score(trial)
                if candidate_score<best_cost-1e-5:
                    best_cost=candidate_score
                    best_trial=(trial,proposed)
        if best_trial is None:
            break
        best_result,groups=best_trial
    return best_result


def _axial_end(p: Project) -> float:
    return axial_u(sum(w.on_axis for w in p.wells)-1)


def _carry_internal_axis_coordinates(source:Project,copy:Project)->Project:
    """Служебная реверсивная СК должна переживать dataclasses.replace().

    Иначе при выборе правой плиты меняются местами физические номера
    скважин: первая оказывается на месте последней. Это также делало
    связи из предварительной схемы несовместимыми с итоговым расчётом.
    """
    for attr in ('_axial_reverse','_axial_length'):
        if hasattr(source,attr):
            setattr(copy,attr,getattr(source,attr))
    return copy


def _mirror_project(p: Project) -> Project:
    """Внутренняя реверсивная система U/V для правого торца.

    Физические положения скважин не меняются: локальные координаты
    (u',v') = (L-u,-v), где L — координата ПОСЛЕДНЕЙ осевой скважины.
    Старый проверенный расчёт всегда видит начало магистрали слева;
    потом готовая сеть возвращается в исходную мировую систему.
    """
    L=_axial_end(p)
    old=Frame(p)
    x,y=old.xy(Pt(L,0))
    q=replace(p,first_x=x,first_y=y,azimuth=(p.azimuth+180.)%360,
              center_position='left_auto')
    # Оба внутренних направления развёрнуты на 180°.
    flip_exit={'u+':'u-','u-':'u+','v+':'v-','v-':'v+','auto':'auto'}
    q.wells=[replace(w,exit_direction=flip_exit[w.exit_direction]) for w in p.wells]
    setattr(q,'_axial_reverse', True)
    setattr(q,'_axial_length', L)
    return q


def _unmirror_result(result: Result, original: Project) -> Result:
    L=_axial_end(original)
    def flip(pt):
        return None if pt is None else Pt(L-pt.u, -pt.v)
    def map_placement(wp):
        flip_exit={'u+':'u-','u-':'u+','v+':'v-','v-':'v+','auto':'auto'}
        return replace(wp,point=flip(wp.point),connection_target=flip(wp.connection_target),
                       well=replace(wp.well,exit_direction=flip_exit[wp.well.exit_direction]))
    result.wells=[map_placement(wp) for wp in result.wells]
    result.center=flip(result.center)
    result.tie=flip(result.tie)
    result.fire=flip(result.fire)
    result.segments=[replace(s,a=flip(s.a),b=flip(s.b)) for s in result.segments]
    result.lanes={m:-v for m,v in result.lanes.items()}
    result.tie_ports={m:flip(pt) for m,pt in result.tie_ports.items()}
    if result.central_plate is not None:
        result.central_plate=result.central_plate.flipped(L)
    result.slabs=[replace(s,u_start=L-s.u_end,u_end=L-s.u_start,
                          v_min=-s.v_max,v_max=-s.v_min) for s in result.slabs]
    result.head_u=L-result.head_u
    result.trunk_end_u=L-result.trunk_end_u
    result.trunk_side=-result.trunk_side
    # В итоговой системе координат зеркальные маршруты и диагностические
    # кандидаты должны показывать одни и те же точки.
    flipped_record_ids=set()
    def flip_record(record):
        if not isinstance(record,dict):return
        # _direct_baseline намеренно хранит те же объекты, что и основные
        # записи. Без проверки identity одно и то же решение зеркалится
        # ДВАЖДЫ, а точка врезки в диагностике оказывается не там.
        identity=id(record)
        if identity in flipped_record_ids:return
        flipped_record_ids.add(identity)
        if record.get('choice_path'):
            record['choice_path']=[(L-u,-v) for u,v in record['choice_path']]
        for candidate in record.get('alternatives',[]):
            loc=candidate.get('target')
            if loc is not None:
                candidate['target']=(L-loc[0],-loc[1])
        flip_port={'U+':'U-','U-':'U+','V+':'V-','V-':'V+'}
        for port in record.get('terminal_approaches',[]):
            port['direction']=flip_port.get(port['direction'],port['direction'])
    for key,val in result.decision_trace.items():
        if key=='_direct_baseline':
            for item in val.values():flip_record(item)
        elif key=='_orientation_candidates':
            invert={'u+':'u-','u-':'u+','v+':'v-','v-':'v+','auto':'auto'}
            for record in val:
                record['exits']={n:invert.get(direction,direction)
                                 for n,direction in record.get('exits',{}).items()}
                for line in record.get('line_candidates',{}).values():
                    flip_record(line)
        elif isinstance(key,tuple):
            flip_record(val)
    result.project=original
    result.frame=Frame(original)
    result.notes=[t for t in result.notes if not t.startswith('Торец центральной плиты у хребта закреплён на U')]
    result.notes.insert(0,f'Торец центральной плиты у хребта закреплён за последней осевой скважиной на U={result.head_u:.2f} м.')
    return result


def _ensure_fitting_support(result: Result):
    """Финальный проход ПОСЛЕ оптимизации: опоры фасонных элементов.

    Не запускаем на каждом пробном маршруте (иначе поиск сильно замедляется).
    Геометрия труб, J и выбор положения центра не меняются.
    """
    from fitting_support import complete_support, unsupported_sites
    from trunk_tap_plates import create_trunk_tap_plates
    if not result.project.auto_rotate and any(not w.well.on_axis for w in result.wells):
        result.notes.append('Автоповорот внеосевых скважин выключен: фиксированное направление '
                            'выхода может требовать дополнительных отводов. Проверьте параметр '
                            '«Разрешить автоматический поворот внеосевых скважин».')
    if result.central_plate is not None:
        trunk_plates=create_trunk_tap_plates(result.segments,result.slabs,
                                              result.central_plate)
        result.slabs.extend(trunk_plates)
        result.metrics['trunk_tap_plate_count']=float(len(trunk_plates))
        if trunk_plates:
            result.notes.append(f'Соединительные хребтовые плиты под внешними '
                                f'тройниками и пересечениями: {len(trunk_plates)} шт.')
    added,removed,sites,missing=complete_support(result.segments,result.slabs,strict=True)
    if removed:
        result.slabs=[s for s in result.slabs if s not in removed]
    result.slabs.extend(added)
    result.metrics['fitting_site_count']=float(len(sites))
    result.metrics['fitting_support_plate_count']=float(sum(x.slab_type=='fitting_support' for x in added))
    result.metrics['fitting_unsupported_count']=float(len(missing))
    result.metrics['transit_run_plate_count']=float(sum(s.slab_type=='transit_run' for s in result.slabs))
    from network_cost import count_small_plates, estimate_score
    result.metrics['small_plate_count']=float(count_small_plates(result.slabs))
    result.metrics['optimized_score']=estimate_score(result)
    if added:
        result.notes.append(f'Опоры фасонных элементов: закреплено {int(result.metrics["fitting_support_plate_count"])} опорных плит; '
                            f'пересчитано {int(result.metrics["transit_run_plate_count"])} регулярных плит, прежних снято {len(removed)}. '
                            'Каждый отвод, тройник и пересечение на плите или в 0,25 м от неё.')
        result.notes.append('Если принудительная плита конфликтовала с равномерным шагом 2–4 м, '
                            'приоритет отдан обязательной опоре узла; проверьте шаг соседних плит.')
    else:
        result.notes.append(f'Опоры фасонных элементов: все {len(sites)} точек '
                            'расположены на плитах или не дальше 0,25 м от их края.')


def _finalize_connection_topology(chosen: Result):
    """Тройники у ближайших плит и запрет четырёхсторонних соединений."""
    from topology_rules import relocate_fourway_branches, four_way_nodes
    from junction_nudging import move_taps_to_existing_trunk_slabs
    adjusted,shifts=move_taps_to_existing_trunk_slabs(chosen.segments,
                                                       chosen.slabs,chosen.wells)
    changes=[]
    if four_way_nodes(adjusted):
        corrected,changes=relocate_fourway_branches(adjusted,chosen.wells)
        if four_way_nodes(corrected):
            raise ValueError('В результате остались недопустимые четверники.')
        adjusted=corrected
    forbidden=inner_trunk_transit_conflicts(adjusted,chosen.wells,chosen.lanes,
                                            chosen.trunk_side,chosen.head_u)
    if forbidden:
        s=forbidden[0]
        raise ValueError(f'После корректировки тройников транзит {MEDIA_RU[s.medium]} '
                         'оказался между хребтом и скважинами. Требуется другой маршрут.')
    guard_bad=protected_well_conflicts(adjusted,chosen.wells)
    if guard_bad:
        no,medium,purpose=guard_bad[0]
        raise ValueError(f'После корректировки тройников участок {MEDIA_RU[medium]} '
                         f'пересёк 5-метровую зону скважины №{no}: {purpose}')
    if not changes and not shifts:
        return
    chosen.segments=merge_segments(adjusted)
    chosen.metrics.pop('fitting_site_count',None)  # geometry changed; invalidate old supports
    if four_way_nodes(chosen.segments):
        raise ValueError('После объединения сегментов образовался запрещённый четверник.')
    from network_cost import network_fittings, transit_corridor_overlap, estimate_score
    fitting=network_fittings(chosen.segments)
    chosen.metrics['fourway_count']=0.0
    chosen.metrics['topological_tees']=float(fitting['tees'])
    chosen.metrics['topological_elbows']=float(fitting['elbows'])
    for med in MEDIA:
        for phase in ('active','future'):
            chosen.stats[med][phase]=sum(seg.length for seg in chosen.segments
                                           if seg.medium==med and seg.phase==phase)
        chosen.stats[med]['total']=chosen.stats[med]['active']+chosen.stats[med]['future']
    chosen.metrics['pipe_m']=sum(s.length for s in chosen.segments)
    chosen.crossings=collision_notes(chosen.segments)
    chosen.metrics['crossings']=float(len(chosen.crossings))
    chosen.metrics['overlaps']=float(sum('Наложение осей' in note for note in chosen.crossings))
    chosen.metrics['foreign_contacts']=float(len(forbidden_foreign_contacts(chosen.segments,chosen.head_u)))
    chosen.metrics['transit_corridor_m']=transit_corridor_overlap(chosen.segments,chosen.project.lane_gap)
    chosen.metrics['optimized_score']=estimate_score(chosen)
    if changes:
        chosen.notes.append('Исправлены четырёхсторонние соединения: '+
                            ', '.join(f'{med}: ({q[0]:.2f}, {q[1]:.2f}) → ({dest[0]:.2f}, {dest[1]:.2f})'
                                      for med,q,dest in changes))
    if shifts:
        chosen.notes.append('Тройники транзитов сдвинуты к хребтовым плитам '
                            'без добавления отводов: '+
                            ', '.join(f'{med}: U={a:.2f} → U={b:.2f}' for med,a,b,_v in shifts))
    # Плиты на изменённой части трассы должны быть перестроены с нуля.
    chosen.slabs=[slab for slab in chosen.slabs if slab.slab_type not in
                  ('transit_run','fitting_support')]
    from axial_plates import create_transit_run_plates
    chosen.slabs.extend(create_transit_run_plates(chosen.segments,chosen.slabs))
    # Окончательная проверка может удалить старые конструктивные опоры.
    # Нельзя оставлять признак уже проверенной геометрии: будет неверная J.
    for field_name in ('fitting_site_count','fitting_support_plate_count',
                       'fitting_unsupported_count'):
        chosen.metrics.pop(field_name,None)


def _finish_crossing_analysis(chosen: Result, border: float):
    _finalize_connection_topology(chosen)
    from crossing_markers import analyze
    markup=analyze(chosen.segments, border,
                   axial_well_ids=[wp.well.number for wp in chosen.wells if wp.well.on_axis])
    chosen.crossing_markup=markup
    chosen.metrics['crossing_marker_count']=len(markup.markers)
    chosen.metrics['crossing_group_count']=markup.grouped_count
    chosen.metrics['crossing_unmarked_count']=len(markup.unassigned)
    chosen.metrics['pipe_3d_m']=chosen.metrics['pipe_m']
    chosen.notes.append(f'Условные участки пересечений: {len(markup.markers)} отметок, '
                        f'объединённых участков {markup.grouped_count}, '
                        f'не размечено {len(markup.unassigned)}. Высотные решения не строятся.')


def _build_auto_positions(p:Project, *, _skip_final_support:bool=False)->Result:
    """Полноценный перебор 4 углов, а не перемещение пиктограммы центра.

    В каждом кандидате заново строятся хребет, ветки, транзиты и точки
    присоединения. Жёсткие ограничения учитываются до значения J.
    """
    L=_axial_end(p)
    frame=Frame(p)
    requested=p.center_position
    sides={'left_upper':1,'left_lower':-1,'right_upper':-1,'right_lower':1}
    if p.center_x is not None:
        manual=frame.uv(p.center_x,p.center_y)
        if -1e-5 <= manual.u <= L+1e-5:
            raise ValueError('Центральный узел должен быть слева от первой или справа от последней осевой скважины')
        positions=(('right_upper' if manual.v>0 else 'right_lower') if manual.u>L
                   else ('left_upper' if manual.v>0 else 'left_lower'),)
    elif requested=='left_auto':
        positions=('left_upper','left_lower')
    elif requested=='auto':
        positions=('left_upper','left_lower','right_upper','right_lower')
    else:
        positions=(requested,)
    candidates=[]
    problems=[]
    for position in positions:
        right=position.startswith('right_')
        sign=sides[position]
        # Если пользователь принудительно зафиксировал сторону хребта,
        # она ограничивает и положение центра (не переопределяем молча).
        if p.trunk_side!='auto' and p.center_x is None and requested in ('auto','left_auto'):
            permitted=1 if p.trunk_side=='positive' else -1
            external_sign=-sign if right else sign
            # desired sign in original reference, not internal mirror
            if external_sign != permitted:
                continue
        q=_mirror_project(p) if right else replace(p)
        q.center_position='left_auto'
        internal_sign=sign
        q.trunk_side='positive' if internal_sign==1 else 'negative'
        try:
            trial=build(q,_skip_final_support=_skip_final_support)
            if right:
                trial=_unmirror_result(trial,p)
            else:
                trial.project=p
            trial.selected_center_position=position
            trial.notes.insert(0,'Положение центрального узла: '+{
                'left_upper':'слева сверху','left_lower':'слева снизу',
                'right_upper':'справа сверху','right_lower':'справа снизу'}[position]+'.')
            candidates.append(trial)
        except (ValueError, AssertionError) as e:
            problems.append(position+': '+str(e))
    if not candidates:
        raise ValueError('Ни одна выбранная позиция центрального узла не даёт допустимой трассы. '
                         +' | '.join(problems[:4]))
    # Готовые сети сравниваем в их исходных географических координатах.
    # Контакты разных сред / наложения важнее экономии метража.
    chosen=min(candidates,key=lambda r:(r.metrics.get('foreign_contacts',0),
                                        r.metrics.get('overlaps',0),score(r),
                                        positions.index(r.selected_center_position)))
    if len(candidates)>1:
        names={'left_upper':'слева-сверху','left_lower':'слева-снизу',
               'right_upper':'справа-сверху','right_lower':'справа-снизу'}
        chosen.notes.append('Сравнены положения центра: '+ '; '.join(
            f'{names[r.selected_center_position]}: J={score(r):.1f}, '
            f'труб={r.metrics["pipe_m"]:.1f} м, пересечений={len(r.crossings)}, '
            f'касаний={r.metrics.get("foreign_contacts",0):.0f}'
            for r in candidates))
    if problems:
        chosen.notes.append('Недопустимые положения центра: '+ '; '.join(problems[:4]))
    _finish_crossing_analysis(chosen,p.bridge_approach)
    if not _skip_final_support and 'fitting_site_count' not in chosen.metrics:
        _ensure_fitting_support(chosen)
    return chosen


def build(p:Project,_overrides=None,_bundles=None,_side=None,_orientations=None,_taps=None,_product_route_mode=None,
          *,_skip_final_support:bool=False)->Result:
    # v91: публичный build() = только осевое ядро.
    # Старый полный перебор внешних трасс/пучков/врезки отключён.
    p.validate()
    if (_overrides is None and _bundles is None and _taps is None
            and _product_route_mode is None and _orientations is None and _side is None):
        from axis_core import build_axis_core
        result,_,_=build_axis_core(p)
        return result
    if _overrides is None and p.center_position!='left_auto':
        return _build_auto_positions(p,_skip_final_support=_skip_final_support)
    if p.center_x is not None and Frame(p).uv(p.center_x,p.center_y).u>1e-6:
        raise ValueError('Центральный узел должен находиться ДО первой осевой скважины по направлению НДС')
    if _overrides is None:
        sides=(1,-1) if p.trunk_side=='auto' else ((1,) if p.trunk_side=='positive' else (-1,))
        candidates=[]
        rejections=[]
        for sign in sides:
            # Явно выбранная пользователем сторона выхода сама задаёт
            # ориентацию прямоугольника; запрещать трассу из-за старого
            # сохранённого поворота было бы ошибкой.
            initial_project=_carry_internal_axis_coordinates(p,replace(p,wells=[
                replace(w,orientation=('perpendicular' if w.exit_direction[0]=='u' else 'parallel'))
                if not w.on_axis and w.exit_direction!='auto' else replace(w)
                for w in p.wells]))
            original_orientations={w.number:w.orientation for w in initial_project.wells}
            baseline=None
            try:
                baseline=build(initial_project,_overrides={},_bundles=[],_side=sign,
                               _orientations=original_orientations)
                _finalize_connection_topology(baseline)
            except (ValueError,AssertionError) as exc:
                rejections.append(f"Сторона {'+' if sign==1 else '-'}: {exc}")
                continue

            def optimize_candidate(direct, orient_map, variant_project):
                # При выборе выхода и поворота оптимизируем всю сеть, а не
                # изолированный отрезок от скважины до первого отвода.
                if variant_project.shared_corridors:
                    best=optimize_groups(variant_project,direct,sign,orient_map)
                    best=optimize_tie_corridor(variant_project,best,sign,orient_map)
                else:
                    best=direct
                _finalize_connection_topology(best)
                best.decision_trace['_direct_baseline']={k:v for k,v in direct.decision_trace.items()
                                                        if isinstance(k,tuple)}
                best.decision_trace['_global_comparison']={
                    'direct_J':round(score(direct),5),
                    'optimized_J':round(score(best),5),
                    'direct_elbows':int(direct.metrics.get('topological_elbows',0)),
                    'optimized_elbows':int(best.metrics.get('topological_elbows',0)),
                    'direct_pipe_m':round(direct.metrics.get('pipe_m',0),4),
                    'optimized_pipe_m':round(best.metrics.get('pipe_m',0),4),
                    'direct_small_plates':int(__import__('network_cost').count_small_plates(direct.slabs)),
                    'optimized_small_plates':int(__import__('network_cost').count_small_plates(best.slabs)),
                    'groups':[list(group) for group in best.routing_groups],
                    'note':'Сравнение целых сетей. Полный перебор непрерывных координат тройников математически невозможен.',
                }
                return best

            def preference(result):
                return (result.metrics.get('foreign_contacts',0),
                        result.metrics.get('overlaps',0),
                        round(score(result),7),
                        result.metrics.get('topological_elbows',float('inf')),
                        result.metrics.get('pipe_m',float('inf')))

            def alternative_project(assign):
                ws=[]
                for w in p.wells:
                    direction=assign.get(w.number)
                    if direction and direction!='auto':
                        ws.append(replace(w,orientation=('perpendicular' if direction[0]=='u' else 'parallel'),
                                          exit_direction=direction))
                    else:
                        ws.append(replace(w))
                q=_carry_internal_axis_coordinates(p,replace(p,wells=ws))
                return q,{w.number:w.orientation for w in q.wells}

            remote=[w for w in p.wells if not w.on_axis and w.status!='inactive']
            movable=[]
            choices=[]
            for w in remote:
                if w.exit_direction!='auto':
                    options=(w.exit_direction,)
                elif not p.auto_rotate:
                    options=('auto',)
                elif w.rotation_locked:
                    options=(('v+','v-') if w.orientation=='parallel' else ('u+','u-'))
                else:
                    options=('u+','u-','v+','v-')
                movable.append(w)
                choices.append(options)
            # Полный декартов перебор 4^N допустим на небольших кустах.
            # Более крупные проекты используют ограниченный набор: отдельные
            # изменения и пары, с явной отметкой непроверенных комбинаций.
            all_count=math.prod(len(options) for options in choices)
            if all_count <= 64:
                assignments=[dict(zip((w.number for w in movable),values))
                             for values in iterproduct(*choices)]
                exhaustive_first_pass=True
            else:
                original={w.number:('auto' if w.exit_direction=='auto' else w.exit_direction) for w in movable}
                assignments=[original]
                for w,opts in zip(movable,choices):
                    for direction in opts:
                        changed={**original,w.number:direction}
                        assignments.append(changed)
                for i in range(min(len(movable),8)):
                    for j in range(i+1,min(len(movable),8)):
                        for a in choices[i]:
                            for b in choices[j]:
                                assignments.append({**original,movable[i].number:a,movable[j].number:b})
                assignments=assignments[:96]
                exhaustive_first_pass=False
            seen=set();feasible=[];orientation_trace=[]
            # На самом первом (базовом) варианте есть возможность проверить
            # начальные координаты даже если автоматический подбор выключен.
            base_key=tuple((w.number,w.exit_direction) for w in remote)
            for assign in assignments:
                key=tuple((w.number,assign.get(w.number,'auto')) for w in movable)
                if key in seen:continue
                seen.add(key)
                variant_project,orient=alternative_project(assign)
                info={'orientations':dict(orient), 'exits':dict(assign),
                      'pre_J':None,'final_J':None,'status':'', 'elbows':None}
                try:
                    if all(assign.get(w.number,w.exit_direction)==w.exit_direction for w in movable):
                        direct=baseline
                    else:
                        direct=build(variant_project,_overrides={},_bundles=[],
                                     _side=sign,_orientations=orient)
                        _finalize_connection_topology(direct)
                    info['pre_J']=round(score(direct),5)
                    info['elbows']=int(direct.metrics.get('topological_elbows',0))
                    info['status']='Допустим: предварительная сеть'
                    # Реальные альтернативы для данного НАЧАЛА, пока другие
                    # скважины находятся в комбинации `assign`. Журнал
                    # хранит не только выигравший выход, но и отвергнутые.
                    info['line_candidates']={
                        f'{key[0]}:{key[1]}':{
                            'selected':entry.get('choice'),
                            'alternatives':list(entry.get('alternatives',[])),
                            'rejected':dict(entry.get('rejected',{})),
                        }
                        for key,entry in direct.decision_trace.items()
                        if isinstance(key,tuple) and isinstance(key[0],int)
                    }
                    feasible.append((preference(direct),key,direct,orient,variant_project,info))
                except (ValueError,AssertionError) as exc:
                    info['status']='Недопустимо: '+str(exc)[:180]
                orientation_trace.append(info)
            if not feasible:
                rejections.append('Сторона '+str(sign)+': нет допустимых комбинаций выходов')
                continue
            feasible.sort(key=lambda x:(x[0],x[1]))
            # Для одной или двух внеосевых скважин проверяем полностью ВСЕ
            # допустимые комбинации из 4 направлений выхода.
            if len(movable)<=2 and all_count<=16:
                shortlisted=feasible
            else:
                shortlisted=feasible[:(4 if len(movable)<=3 else 2)]
                base_item=next((it for it in feasible if it[1]==base_key),None)
                if base_item is not None and base_item not in shortlisted:
                    shortlisted.append(base_item)
            trial=None
            for _pref,key,direct,orient,variant_project,info in shortlisted:
                try:
                    candidate=optimize_candidate(direct,orient,variant_project)
                except (ValueError,AssertionError) as exc:
                    info['status']='Отклонён на полном пересчёте: '+str(exc)[:140]
                    continue
                info['final_J']=round(score(candidate),5)
                info['elbows']=int(candidate.metrics.get('topological_elbows',0))
                info['status']='Расчёт J независимых линий (длина + отводы + тройники + пересечения)'
                if trial is None or preference(candidate)<preference(trial):
                    trial=candidate
            if trial is None:
                rejections.append('Сторона '+str(sign)+': все подробные варианты недопустимы')
                continue

            # v74: Compare COMPLETE optimized networks for distinct product
            # attachment families. Previously product side-port alternatives
            # were tested as isolated pipes, before the off-axis corridors
            # were rebuilt around the chosen product riser. That missed a
            # potentially cheaper shared corridor with other media.
            # Only the best already-scored orientation is re-evaluated here;
            # this deliberately is not an exhaustive global proof.
            remote_count=sum(not w.on_axis and w.status!='inactive'
                             for w in trial.project.wells)
            # v74: always compare both allowed PRODUCT attachment families
            # on the whole finished independent network, including the fire
            # line, regardless of the number of off-axis wells.  This is NOT
            # bundle search: _bundles remains empty for every trial.
            if any(s.purpose=='Продуктовый нефтепровод: от центра к врезке'
                   for s in trial.segments):
                candidate_project=trial.project
                candidate_orientations={w.number:w.orientation for w in candidate_project.wells}
                incumbent=score(trial)
                compared=[]
                for product_mode in ('side_safe', 'trunk_nearest'):
                    try:
                        alt_direct=build(candidate_project, _overrides={}, _bundles=[],
                                         _side=sign, _orientations=candidate_orientations,
                                         _product_route_mode=product_mode)
                        # Full optimization is needed even if the isolated
                        # product route is more expensive: bundling could
                        # reverse the ordering of the whole-network scores.
                        alternate=optimize_candidate(alt_direct,candidate_orientations,
                                                       candidate_project)
                        alt_entry=alt_direct.decision_trace.get(('external','oil_product'),{})
                        alt_path=alt_entry.get('choice_path') or []
                        compared.append({'mode':product_mode,
                                         'full_J':round(score(alternate),5),
                                         'port':alt_path[0] if alt_path else None,
                                         'actual_choice':alt_entry.get('choice'),
                                         'shared_groups':[list(g) for g in alternate.routing_groups]})
                        if preference(alternate)<preference(trial):
                            trial=alternate
                            trial.notes.append(
                                'v74: сравнили боковую врезку в центральную плиту и '
                                'врезку в хребет по полной стоимости независимой сети.')
                    except (ValueError,AssertionError) as exc:
                        compared.append({'mode':product_mode,'full_J':None,
                                         'reason':str(exc)[:140]})
                trial.decision_trace['_product_network_families']=[
                    {'mode':'initial_selected', 'full_J':round(incumbent,5),
                     'note':'Стоимость сети до повторного сравнения семейств'},
                    *compared]
                trial.notes.append('v74: альтернативы точки врезки проверены по полной '
                                   'стоимости сети без пучков; непрерывный перебор всех '
                                   'мест подключений математически не гарантирован.')
            examined_orientations=len(orientation_trace)
            full_orientations=sum(record['final_J'] is not None for record in orientation_trace)
            trial.notes.append(
                f'Поиск выходов: сформировано {all_count} комбинаций четырёх сторон, '
                f'проверено предварительно {examined_orientations}, полностью {full_orientations}. '
                f'Для крупных проектов проверка всех 4^N комбинаций ограничена, '
                f'чтобы не блокировать редактор.')
            trial.decision_trace['_start_search_meta']={
                'total_combinations':all_count,
                'prechecked':examined_orientations,
                'full_scored':full_orientations,
                'all_start_combinations_evaluated':exhaustive_first_pass,
                'all_feasible_combinations_full_scored':len(shortlisted)==len(feasible),
            }
            trial.decision_trace['_orientation_candidates']=orientation_trace
            trial.metrics['orientation_candidates_evaluated']=float(examined_orientations)
            trial.metrics['orientation_full_comparisons']=float(full_orientations)
            if p.shared_corridors and trial.routing_groups:
                before_taps=trial
                trial=optimize_same_medium_taps(p,trial,sign)
                for key,value in before_taps.decision_trace.items():
                    if isinstance(key,str) and key.startswith('_'):
                        trial.decision_trace.setdefault(key,value)
            trial.metrics['original_pipe_m']=baseline.metrics['pipe_m']
            trial.metrics['original_corridor_m']=baseline.metrics['shared_corridor_m']
            trial.metrics['original_score']=score(baseline)
            trial.metrics['optimized_score']=score(trial)
            trial.notes.append(f"Перебор стороны {sign:+d} и поворотов скважин: J={score(trial):.2f} условных м.")
            candidates.append(trial)
        if not candidates:
            raise ValueError('Невозможно построить сеть: выбранные положения хребта нарушают защитные зоны скважин (5 м). '
                             +' | '.join(rejections[:2]))
        chosen=min(candidates,key=lambda r:(r.metrics.get('foreign_contacts',0),
                                            score(r),0 if r.trunk_side==1 else 1))
        chosen.selected_center_position=('left_upper' if chosen.trunk_side==1 else 'left_lower')
        # Анализ пересечений не изменяет существующую геометрию трасс.
        # Мы намеренно НЕ рассчитываем 3D-профили, подъём трубы, отводы и метраж.
        _finalize_connection_topology(chosen)
        from crossing_markers import analyze
        markup=analyze(chosen.segments, p.bridge_approach,
                       axial_well_ids=[wp.well.number for wp in chosen.wells if wp.well.on_axis])
        chosen.crossing_markup=markup
        chosen.metrics['crossing_marker_count']=len(markup.markers)
        chosen.metrics['crossing_group_count']=markup.grouped_count
        chosen.metrics['crossing_unmarked_count']=len(markup.unassigned)
        chosen.metrics['pipe_3d_m']=chosen.metrics['pipe_m']  # legacy key: никаких надбавок
        chosen.notes.append(f'Условные участки пересечений: {len(markup.markers)} отметок, '
                            f'объединённых участков {markup.grouped_count}, '
                            f'не размечено {len(markup.unassigned)}. Высотные решения не строятся.')
        if not _skip_final_support:
            _ensure_fitting_support(chosen)
        return chosen
    frame=Frame(p)
    sign=_side if _side in (-1,1) else 1
    tie=frame.uv(p.tie_x,p.tie_y);fire=frame.uv(p.fire_x,p.fire_y)
    # Для внеосевых скважин направление первого выхода определяется
    # положением реального хребта (а не одинаковым знаком side).
    lanes=lane_positions(p,sign)
    axial_index=0;placements=[]
    for w in p.wells:
        if w.on_axis:
            u=axial_u(axial_index)
            if getattr(p,'_axial_reverse',False):
                u=p._axial_length-u
            q=Pt(u,0.);axial_index+=1
            wx,wy=frame.xy(q)
        else:
            wx,wy=float(w.x),float(w.y);q=frame.uv(wx,wy)
        opts=(_overrides or {}).get(w.number, {})
        target=None
        if 'tie_corridor' in opts:
            medium=next((m for m,_ in well_connections(w) if m in opts['media_axes']),
                        next(iter(opts['media_axes'])))
            ax=opts['media_axes'][medium]
            target=(Pt(ax,lanes[medium]) if opts['tie_corridor']=='v'
                    else Pt(-p.head_lead,ax))
        tap_targets=[pair[1] for (n,_),pair in (_taps or {}).items() if n==w.number]
        if len(tap_targets)==1:
            target=tap_targets[0]
        placements.append(WellPlacement(w,q,wx,wy,tuple(well_connections(w)),
                 (_orientations or {}).get(w.number,w.orientation),target))
    # Фиксированный порядок труб уже рассчитан перед размещением скважин.
    # ЦЕНТР ЗАКРЕПЛЁН: либо точная введённая координата,
    # либо фиксированная привязка относительно ПЕРВОЙ ОСЕВОЙ скважины.
    # ВНЕОСЕВЫЕ скважины никогда не влияют на координаты центрального узла.
    if p.center_x is not None:
        center=frame.uv(p.center_x,p.center_y)
        head_u=center.u+PLATE_LENGTH/2
        if head_u > 1e-6:
            raise ValueError('Центральный узел должен находиться ДО первой осевой скважины по направлению НДС')
    else:
        center=Pt(-p.head_lead,lanes['oil_measure'])
        head_u=center.u+PLATE_LENGTH/2
        # Do not move central node when mirroring; connect measuring pipe by short local stub.
    mid_v=center.v
    # С этого момента центральный узел — площадной объект, ориентированный
    # по магистральным трубам. Его внутренняя грань совпадает с началом хребта.
    plate=make_plate(head_u,lanes,PIPE_OD)
    bucket={m:{'active':[],'future':[]} for m in MEDIA}
    for wp in placements:
        for n,(m,direction) in enumerate(wp.connections):
            bucket[m][line_phase(wp,m)].append((wp,n,direction))
        # Замерная магистраль требуется добывающей скважине, однако
        # непосредственное подключение выполняется будущим узлом с арматурой.
        # В текущей геометрии индивидуальные ветки к замерному трубопроводу ЗАПРЕЩЕНЫ.
        if wp.well.kind=='producer' and wp.well.status!='inactive':
            bucket['oil_measure'][line_phase(wp,'oil_measure')].append((wp,-1,'узел не детализирован'))
    # Магистрали, реально присутствующие на центральной плите, нужны
    # для оценки числа пересечений бокового подвода. Не штрафуем
    # пересечение условной позиции отсутствующей газовой/другой линии.
    used_plate_media=[m for m in MEDIA
                      if bucket[m]['active'] or bucket[m]['future'] or m==p.fire_medium]
    segs=[];tails={};tap_notes=[]
    # Варианты отводов определяются ДО длины хребта: единая ветвь может
    # подключаться в другом створе, поэтому хребет не нужно продлевать до
    # проекции каждой внеосевой скважины независимо от оптимального подхода.
    overrides,bundles=_overrides or {},_bundles or []
    branch_ports={};branch_bends=0
    local_decisions={}
    shared_joins={}
    # Сначала определяем реальные концы существующего хребта по его
    # ОСЕВЫМ скважинам, и только после этого подбираем точки врезки
    # внеосевых веток. Порядок строк в проектном JSON значения не имеет.
    # The preliminary trunk was already fixed between both possible central
    # plate locations. Off-axis branches may use its full supported length,
    # including the far end, even before the final trunk segments are emitted.
    fixed_base_for_routes=base_axial_trunk_reaches(placements)
    far_u=max((w.point.u for w in placements if w.well.on_axis),default=0.) + p.head_lead - PLATE_LENGTH/2
    axial_reach={key:max(end_u,far_u) for key,end_u in fixed_base_for_routes.items()}
    # Резервировать пожарный вывод сразу от ОБОИХ возможных торцов,
    # сначала выбрав более близкий по допустимой ортогональной геометрии.
    # Полный выбор после построения всех хребтовых ниток повторяется ниже.
    from fire_connection_rules import (fire_ends,fire_candidate_paths,
                                       route_length,route_bends)
    preliminary_reaches=dict(axial_reach)
    preview_fire_trunk=[]
    for phase_name in ('active','future'):
        end_u=preliminary_reaches.get((p.fire_medium,phase_name))
        if end_u is not None and end_u>head_u+EPS:
            preview_fire_trunk.append(Segment(Pt(head_u,lanes[p.fire_medium]),
                          Pt(end_u,lanes[p.fire_medium]),p.fire_medium,phase_name,'Хребет'))
    provisional_fire_ends=fire_ends(plate,lanes,preview_fire_trunk,p.fire_medium)
    reserved_options=[]
    for ending in provisional_fire_ends:
        for path in fire_candidate_paths(ending,fire):
            if all(protected_line_ok(a,b,placements) for a,b in zip(path,path[1:])):
                reserved_options.append((route_length(path),route_bends(path),path))
    if reserved_options:
        reserved_path=min(reserved_options,key=lambda item:(item[0],item[1]))[2]
        reserved_fire_segments=[Segment(a,b,p.fire_medium,'active','Резерв пожарного вывода')
                    for a,b in zip(reserved_path,reserved_path[1:])]
        fire_reservation=(p.fire_medium,tuple(reserved_fire_segments))
    else:
        # Отсутствие простого резерва не должно запрещать поиск полного обхода.
        reserved_fire_segments=[]
        fire_reservation=None
    arranged=([wp for wp in placements if wp.well.on_axis]
              +[wp for wp in placements if not wp.well.on_axis])
    for wp in arranged:
        opts=overrides.get(wp.well.number)
        if wp.well.on_axis:
            # Отпайки осевых скважин — фиксированная часть топологии.
            # Групповые оптимизации и любые _overrides не действуют.
            part,ports,bends=route_of_well(
                wp,lanes,head_u=head_u,side=sign,gap=p.lane_gap,
                all_wells=placements,existing=segs,
                mirror_tangent=getattr(p,'_axial_reverse',False))
        elif opts and 'tie_corridor' in opts:
            taps_for_wp={m:pair for (n,m),pair in (_taps or {}).items() if n==wp.well.number}
            part,ports,bends,joins=route_well_via_tie(wp,lanes,opts,head_u,sign,p.lane_gap,
                         placements,p.elbow_cost/max(p.pipe_cost_per_m,0.001),segs,
                         taps=taps_for_wp,planned_shared_joins=shared_joins,central_plate=plate)
            for m,junction in joins.items():
                shared_joins.setdefault(m,[]).append((junction,line_phase(wp,m),opts['tie_corridor'],wp.well.number))
            for med,_ in wp.connections:
                if med=='oil_measure':continue
                local_decisions[(wp.well.number,med)]={
                    'source':'tie_corridor_override', 'well':wp.well.number,
                    'medium':med,'orientation':wp.orientation,
                    'choice':'Коридор, выбранный оптимизатором общей сети',
                    'choice_cost':None,'choice_path':[],
                    'alternatives':[],'rejected':{},
                    'note':'Схема выбрана глобальным оптимизатором пучков; локальные варианты в этом повторном построении не оценивались и не сохранялись.',
                }
        elif opts:
            # Смещения созданы одинаковым методом и для всех членов группы.
            offsets=opts['offsets']
            part,ports,bends=route_of_well(wp,lanes,opts['center'],offsets,opts.get('line_offsets'),head_u,sign,p.lane_gap,
                   all_wells=placements,staging_v=opts.get('staging_v'),
                   bend_penalty=p.elbow_cost/max(p.pipe_cost_per_m,0.001),existing=segs,central_plate=plate,
                   crossing_penalty=0.0,  # v74: не штрафуем условные 2D-пересечения
                   plate_media=used_plate_media,fire_reservation=fire_reservation,
                   decision_sink=local_decisions)
        else:
            available_reach={}
            if not wp.well.on_axis:
                for med,_ in wp.connections:
                    if med=='oil_measure':continue
                    active=axial_reach.get((med,'active'))
                    future=axial_reach.get((med,'future'))
                    if line_phase(wp,med)=='future':
                        reach_values=[q for q in (active,future) if q is not None]
                    else:
                        reach_values=[active] if active is not None else []
                    if reach_values:available_reach[med]=max(reach_values)
            part,ports,bends=route_of_well(wp,lanes,line_offsets=None,head_u=head_u,side=sign,gap=p.lane_gap,
                   all_wells=placements,bend_penalty=p.elbow_cost/max(p.pipe_cost_per_m,0.001),existing=segs,
                   trunk_reach=available_reach,central_plate=plate,
                   crossing_penalty=0.0,  # v74: не штрафуем условные 2D-пересечения
                   plate_media=used_plate_media,fire_reservation=fire_reservation,
                   decision_sink=local_decisions)
        if opts and not wp.well.on_axis:
            for med,_ in wp.connections:
                if med=='oil_measure':continue
                local_decisions.setdefault((wp.well.number,med),{
                    'source':'group_routing_override','well':wp.well.number,'medium':med,
                    'orientation':wp.orientation,'choice':'Общий транзитный коридор',
                    'choice_cost':None,'choice_path':[],
                    'alternatives':[],'rejected':{},
                    'note':'Коридор задан результатом оптимизации сети; варианты исходного локального поиска здесь не пересчитывались.',
                })
        # Ближайшая существующая линия может быть выбрана ещё на базовом
        # этапе (не только отдельным постоптимизатором). Фиксируем источник
        # присоединения, чтобы журнал не терял полезную диагностику.
        if not wp.well.on_axis and any('к существующей нитке' in seg.purpose for seg in part):
            donor_numbers=set()
            for branch in part:
                if 'к существующей нитке' not in branch.purpose:continue
                for prev in segs:
                    if prev.medium!=branch.medium:continue
                    if not any(distance_segment_to_point(prev.a,prev.b,endpoint)<1e-5
                               for endpoint in (branch.a,branch.b)):
                        continue
                    match=re.search(r'скв\.\s*(\d+)',prev.purpose,re.IGNORECASE)
                    if match and int(match.group(1))!=wp.well.number:
                        donor_numbers.add(int(match.group(1)))
            if donor_numbers:
                tap_notes.append('Скважина №%s: присоединена к существующей нитке скважины №%s.' %
                                 (wp.well.number,min(donor_numbers)))
            else:
                tap_notes.append(f'Скважина №{wp.well.number}: присоединена к существующей нитке своей среды.')
        branch_bends+=bends
        segs.extend(part)
        for med,loc in ports.items():
            branch_ports[(wp.well.number,med)]=loc
            if wp.well.on_axis:
                phase=line_phase(wp,med)
                axial_reach[(med,phase)]=max(loc,axial_reach.get((med,phase),head_u))
    if shared_joins:
        branch_bends+=add_shared_tie_backbones(segs,shared_joins,lanes,head_u,placements,central_plate=plate)
    inner_bad=inner_trunk_transit_conflicts(segs,placements,lanes,sign,head_u)
    if inner_bad:
        seg=inner_bad[0]
        raise ValueError(f'Транзит {MEDIA_RU[seg.medium]} проходит вдоль хребта со стороны '
                         f'скважин (U={seg.a.u:.2f}..{seg.b.u:.2f}, V={seg.a.v:.2f}). '
                         'Нужен обход снаружи хребта и 5-метровых зон.')
    foreign=protected_well_conflicts(segs,placements)
    if foreign:
        no,m,purpose=foreign[0]
        raise ValueError(f'Запретная зона скважины №{no}: участок {MEDIA_RU[m]} ({purpose}). '
                         'Измените компоновку или сторону хребта.')
    fixed_trunk_reaches=base_axial_trunk_reaches(placements)
    # Варианты центральной плиты (слева/справа) должны присоединяться
    # к ОДНОМУ физически неизменному хребту. Поэтому для всех осевых
    # сред его базовый конец доводится до внутреннего торца возможной
    # плиты на противоположном конце ряда. После этого выбор центра
    # не может сократить или сдвинуть показанные на первом этапе нитки.
    far_head=max((wp.point.u for wp in placements if wp.well.on_axis),default=0.) + p.head_lead - PLATE_LENGTH/2
    fixed_trunk_reaches={key:max(u,far_head) for key,u in fixed_trunk_reaches.items()}
    # Один хребет: у каждой необходимой среды свой параллельный трубопровод.
    # Длина хребтового трубопровода определяется положением ПОСЛЕДНЕГО
    # фактического подключения, а не проекцией удалённой скважины.
    for m in MEDIA:
        active=bucket[m]['active'];future=bucket[m]['future']
        if not active and not future:
            tails[m]=(None,None);continue
        def furthest(rows):
            if not rows:return None
            us=[wp.point.u if m=='oil_measure' else branch_ports[(wp.well.number,m)]
                for wp,_,_ in rows]
            return max(us)
        active_u=furthest(active)
        future_u=furthest(future)
        # Предварительно показанная нитка — несдвигаемая основа сети.
        # Продление допустимо, укорачивание ниже базового конца — нет.
        if (m,'active') in fixed_trunk_reaches:
            active_u=max(active_u if active_u is not None else head_u,
                         fixed_trunk_reaches[(m,'active')])
        if (m,'future') in fixed_trunk_reaches:
            future_u=max(future_u if future_u is not None else head_u,
                         fixed_trunk_reaches[(m,'future')])
        v=lanes[m]
        if active_u is not None:
            add(segs,Pt(head_u,v),Pt(max(head_u,active_u),v),m,'active','Хребет')
        if future_u is not None:
            fut_end=max(head_u,future_u)
            start=max(head_u,active_u) if active_u is not None else head_u
            if fut_end>start+EPS:
                add(segs,Pt(start,v),Pt(fut_end,v),m,'future','Перспективное продление хребта')
            elif active_u is None:
                add(segs,Pt(head_u,v),Pt(fut_end,v),m,'future','Перспективный хребет')
        tails[m]=(active_u,future_u)
    # Центральная плита показывает только реально используемые среды.
    # Замерный нефтепровод НЕ имеет никаких внешних присоединений.
    installed_phase={m:('active' if bucket[m]['active'] else
                        'future' if bucket[m]['future'] else 'active') for m in used_plate_media}
    segs.extend(central_pipe_segments(plate,lanes,used_plate_media,installed_phase))
    # Auto-tie: perpendicular through center, at HALF the NDS axis length
    # measured from the first to the last axial well (not the total pipe length).
    # Manual/confirmed tie coordinates remain untouched.
    if not p.tie_confirmed:
        axial_count=sum(w.on_axis for w in p.wells)
        nds_length=axial_u(axial_count-1) if axial_count>1 else 0.0
        tie_distance=max(0.1,nds_length*0.5)
        used=[lanes[m] for m in used_plate_media]
        if not used:used=[lanes['oil_product']]
        options=(Pt(center.u,tie_distance),Pt(center.u,-tie_distance))
        tie=min(options,key=lambda q:(min(abs(q.v-v) for v in used),abs(q.v-center.v)))
    # К ТОЧКЕ ВРЕЗКИ ИДЕТ ТОЛЬКО ПРОДУКТОВЫЙ НЕФТЕПРОВОД.
    # Замерная нитка заканчивается в центральном узле И НЕ СОЕДИНЯЕТСЯ
    # с внешними линиями. Геометрическая точка центра находится на
    # полосе замерной нитки, поэтому подключать продуктовую внешнюю
    # трубу непосредственно к этой точке запрещено. Порт продуктовой
    # сети — Pt(head_u, lanes['oil_product']), отдельный от замерного.
    # Старые флажки tie_media из v3 не влияют на этот маршрут.
    tie_ports={}
    # External risers are intentionally staggered by the same clear distance
    # as the trunk. No two different systems may share the same riser axis.
    exterior=('oil_product',p.fire_medium)
    raw_offsets=bundle_offsets(exterior,p.lane_gap)
    first=min(raw_offsets.values())
    ports={m:plate.outer_face_u-1.5-(raw_offsets[m]-first) for m in exterior}

    def evaluate_external(route,m,phase,purpose='Транзитный трубопровод'):
        trial=list(segs)
        for a,b in zip(route,route[1:]):
            add(trial,a,b,m,phase,purpose)
        # Кандидаты через запретные зоны или создающие новый ложный контакт
        # концов разных сред недопустимы, независимо от стоимости.
        if protected_well_conflicts(trial,placements):return math.inf
        if inner_trunk_transit_conflicts(trial,placements,lanes,sign,head_u):return math.inf
        from topology_rules import four_way_nodes
        if four_way_nodes(trial):return math.inf
        if len(forbidden_foreign_contacts(trial,head_u)) > len(forbidden_foreign_contacts(segs,head_u)):
            return math.inf
        # Include the already reserved independent fire route in the
        # preliminary economic comparison; otherwise a product detour that
        # looks cheap locally can make the final combined network expensive.
        cost_network=[*trial,*reserved_fire_segments] if m=='oil_product' else trial
        network=merge_segments(cost_network)
        errors=collision_notes(network)
        overlaps=sum('Наложение' in x for x in errors)
        # Strong preference to remove overlapped axes of distinct media.
        # On a cost tie prefer extension (first candidate), not arbitrary bends.
        from network_cost import transit_corridor_overlap, network_fittings
        fittings=network_fittings(network)
        return (p.pipe_cost_per_m*sum(s.length for s in network)
                +p.elbow_cost*fittings['elbows']+p.tee_cost*fittings['tees']
                # Условные 2D-пересечения отображаются отдельно; до расчёта
                # высотных переходов их условная цена не должна притягивать
                # независимую линию к длинному маршруту с лишним коленом.
                +0.0*len(errors)
                -(p.corridor_saving_per_m*transit_corridor_overlap(network,p.lane_gap) if p.shared_corridors else 0.0)
                +1e5*overlaps)

    # Длина точного совпадения прямых участков одной среды. Точечное
    # пересечение под 90° не является совместным коридором.
    def shared_axis_overlap(a:Segment,b:Segment)->float:
        if a.horizontal != b.horizontal:
            return 0.0
        if a.horizontal:
            if abs(a.a.v-b.a.v)>1e-5:return 0.0
            alo,ahi=sorted((a.a.u,a.b.u));blo,bhi=sorted((b.a.u,b.b.u))
        else:
            if abs(a.a.u-b.a.u)>1e-5:return 0.0
            alo,ahi=sorted((a.a.v,a.b.v));blo,bhi=sorted((b.a.v,b.b.v))
        return max(0.,min(ahi,bhi)-max(alo,blo))

    def external_bends(route):
        clean=[route[0]]
        for point in route[1:]:
            if point != clean[-1]:clean.append(point)
        return sum(1 for a,b,c in zip(clean,clean[1:],clean[2:])
                   if (abs(a.u-b.u)<EPS)!=(abs(b.u-c.u)<EPS))

    def four_terminal_approaches(route,node):
        """Четыре возможных направления входа в точку/узел.

        Возвращаем корректные ортогональные терминальные маршруты и
        обозначение подхода; реальные препятствия проверяются позже.
        """
        if len(route)<2:return []
        previous=route[-2]
        prefix=list(route[:-1])
        points=(('U+',Pt(node.u+.6,node.v)),('U-',Pt(node.u-.6,node.v)),
                ('V+',Pt(node.u,node.v+.6)),('V-',Pt(node.u,node.v-.6)))
        attempts=[];seen=set()
        for label,approach in points:
            for mid in (Pt(approach.u,previous.v),Pt(previous.u,approach.v)):
                nodes=prefix[:]
                for pt in (mid,approach,node):
                    if nodes[-1]!=pt:nodes.append(pt)
                # Нет диагоналей, возвратных шагов и нулевых фасонных узлов.
                if any(abs(b.u-a.u)>EPS and abs(b.v-a.v)>EPS for a,b in zip(nodes,nodes[1:])):
                    continue
                if any(((b.u-a.u)*(c.u-b.u)+(b.v-a.v)*(c.v-b.v)) < -EPS
                       and ((abs(a.u-b.u)<EPS and abs(b.u-c.u)<EPS) or
                            (abs(a.v-b.v)<EPS and abs(b.v-c.v)<EPS))
                       for a,b,c in zip(nodes,nodes[1:],nodes[2:])):
                    continue
                key=tuple(nodes)
                if key in seen:continue
                seen.add(key)
                attempts.append((label,tuple(nodes)))
        # Для обратного направления к конечной точке простой L-поворот
        # может пройти через сам узел и вернуться назад. В этом случае
        # пробуем полноценный обход с двумя коленами за пределами узла.
        already={label for label,_ in attempts}
        for label,approach in points:
            if label in already:continue
            for offset in (1.5,-1.5):
                if label.startswith('U'):
                    border_v=node.v+offset
                    middle=(Pt(previous.u,border_v),Pt(approach.u,border_v))
                else:
                    border_u=node.u+offset
                    middle=(Pt(border_u,previous.v),Pt(border_u,approach.v))
                nodes=prefix[:]
                for pt in (*middle,approach,node):
                    if nodes[-1]!=pt:nodes.append(pt)
                if any(abs(b.u-a.u)>EPS and abs(b.v-a.v)>EPS for a,b in zip(nodes,nodes[1:])):
                    continue
                if any(((b.u-a.u)*(c.u-b.u)+(b.v-a.v)*(c.v-b.v)) < -EPS
                       and ((abs(a.u-b.u)<EPS and abs(b.u-c.u)<EPS) or
                            (abs(a.v-b.v)<EPS and abs(b.v-c.v)<EPS))
                       for a,b,c in zip(nodes,nodes[1:],nodes[2:])):
                    continue
                attempts.append((label,tuple(nodes)))
                already.add(label)
                break
        return attempts

    # Врезка подключается к ПОРТУ ПРОДУКТОВОГО нефтепровода — не к
    # условному центру (он совпадает с концом замерной магистрали).
    m='oil_product'
    if bucket[m]['active'] or bucket[m]['future']:
        tie_ports[m]=tie
        phase='active' if bucket[m]['active'] else 'future'
        from product_tie_rules import (enumerate_tie_sites,central_side_sites,
                 candidate_gates,end_outward_sign)
        from axial_plates import create_axial_junction_plates,create_intermediate_trunk_plates
        # Опорная геометрия вычисляется из УЖЕ ФИКСИРОВАННЫХ осевых
        # присоединений, без запуска всего этапа «Плиты».
        # Кандидаты на хребте разрешены только на реальных 3/6-м плитах.
        early_junctions,_=create_axial_junction_plates(
            placements,branch_ports,segs,lanes,plate,MEDIA)
        early_central=Slab(plate.u_min,plate.u_max,plate.v_min,plate.v_max,
             phase,1,('oil_product',),'central')
        early_intermediate=create_intermediate_trunk_plates(
            [early_central,*early_junctions],segs,lanes,plate)
        product_trunk=[s for s in segs if s.medium==m and s.purpose in
                       ('Хребет','Перспективный хребет','Перспективное продление хребта')]
        occupied=[float(u) for (number,medium),u in branch_ports.items()
                  if any(wp.well.number==number and wp.well.on_axis for wp in placements)]
        sites=[*central_side_sites(plate,lanes),
               *enumerate_tie_sites(product_trunk,
                        [early_central,*early_junctions,*early_intermediate],
                        plate,occupied,required_phase=phase)]
        product_candidates=[]
        product_families=[]  # side / trunk / end, для режимов пост-сравнения
        def append_product(route,family):
            clean=tuple(q for i,q in enumerate(route) if i==0 or q!=route[i-1])
            if len(clean)<2 or any(abs(a.u-b.u)>EPS and abs(a.v-b.v)>EPS
                                    for a,b in zip(clean,clean[1:])):
                return
            if clean in product_candidates:return
            product_candidates.append(clean)
            product_families.append(family)
        for site in sites:
            if site.kind=='end':
                sign_out=end_outward_sign(plate,site.point)
                out=Pt(site.point.u+sign_out*0.65,site.point.v)
                # Только свободный торец ХРЕБТА: первое плечо направлено
                # наружу от центрального модуля, а не через его торец.
                if (tie.u-site.point.u)*sign_out>=0:
                    append_product((site.point,Pt(tie.u,site.point.v),tie),'end')
                append_product((site.point,out,Pt(out.u,tie.v),tie),'end')
            else:
                for gate in candidate_gates(site,trunk_side=sign,source_v=tie.v):
                    # Первое плечо строго перпендикулярно продуктовой нитке.
                    # Перед продольным обходом труба должна выйти за край плиты.
                    append_product((site.point,gate,Pt(tie.u,gate.v),tie),
                                   'side' if site.plate_type=='central' else 'trunk')
                    if ((gate.v<site.v_min and tie.v<site.v_min) or
                        (gate.v>site.v_max and tie.v>site.v_max)):
                        append_product((site.point,Pt(site.point.u,tie.v),tie),
                                   'side' if site.plate_type=='central' else 'trunk')

        # Дополнительная топология: продуктовая нитка выходит из БОКОВОЙ
        # грани плиты, обходит её по внешней полосе и поворачивает к
        # точке врезки. Это позволяет разместить ППД/СППВ параллельно
        # продуктовой вертикали без пересечения продуктового колена.
        # Выбор фазы «edge_far» служебный: сравнивается со всей сетью,
        # а не выбирается по локальной цене одного продуктового маршрута.
        edge_far=[]
        if tie.u < plate.u_min-0.35 or tie.u > plate.u_max+0.35:
            for site in sorted((site for site in sites if site.plate_type=='central'),
                               key=lambda q:abs(q.point.u-plate.center_u))[:1]:
                u=site.point.u
                port=site.point
                far_v=(plate.v_max+0.5 if tie.v>=plate.center_v else plate.v_min-0.5)
                route=(port,Pt(u,far_v),Pt(tie.u,far_v),tie)
                cost=evaluate_external(route,m,phase)
                if math.isfinite(cost):
                    edge_far.append((cost,route))
        # Отдельная нитка от продуктовой полосы: в некоторых случаях
        # короткий поперечный маршрут имеет смысл, но он никогда не
        # становится частью замерной полосы.
        valid=[]
        for i,route in enumerate(product_candidates):
            cost=evaluate_external(route,m,phase)
            if math.isfinite(cost):valid.append((cost,i,route))
        # A route to a far-away trunk tee may be formally valid but many
        # times longer than the local side port. Previously the safe search
        # ran ONLY when no valid trunk candidate existed; this could produce
        # a needless loop around an entire row of wells. Compare against a
        # geometric lower bound and search side-port detours when warranted.
        def route_len(nodes):
            return sum(abs(b.u-a.u)+abs(b.v-a.v) for a,b in zip(nodes,nodes[1:]))
        direct_side_options=[route for i,route in enumerate(product_candidates)
                             if product_families[i]=='side']
        shortest_possible=min((route_len(nodes) for nodes in direct_side_options),default=math.inf)
        shortest_valid=min((route_len(nodes) for _score,_idx,nodes in valid),default=math.inf)
        detour_threshold=max(6.0,1.5*p.elbow_cost/max(p.pipe_cost_per_m,.001))
        if not valid or shortest_valid > shortest_possible+detour_threshold:
            # Безопасные обходы допускаются ТОЛЬКО от разрешённых
            # боковых точек опор (не из случайной координаты по длине трубы).
            # Применяем малый порядок обхода по близости к врезке;
            # базовый перебор всех 0,1-м точек уже сделан выше.
            nearest_sites=sorted((site for site in sites if site.kind=='side'),
                                 key=lambda site:(abs(site.point.u-tie.u),
                                                  0 if site.plate_type=='central' else 1))
            detour_sites=[];seen_sites=set()
            for site in nearest_sites:
                if site.point.u in seen_sites:continue
                seen_sites.add(site.point.u)
                detour_sites.append(site)
                if len(detour_sites)>=16:break
            for site in detour_sites:
                for gate in candidate_gates(site,trunk_side=sign,source_v=tie.v):
                    try:
                        safe_tail=rectilinear_safe_path(gate,tie,placements,
                            bend_penalty=p.elbow_cost/max(p.pipe_cost_per_m,0.001),
                            existing=segs,medium=m,
                            inner_zone=trunk_inner_zone(lanes,sign,head_u,placements))
                    except ValueError:
                        continue
                    route=(site.point,*safe_tail)
                    cost=evaluate_external(route,m,phase)
                    if math.isfinite(cost):
                        valid.append((cost,len(product_candidates),route))
        from path_preference import tie_network_key
        if _product_route_mode=='edge_far' and edge_far:
            route=min(edge_far,key=lambda row:tie_network_key(row[0],row[1]))[1]
        elif _product_route_mode=='side_safe' and valid:
            # Forced candidate family used only for the post-routing FULL
            # network cost comparison. No end-face product attachment.
            side_indices={i for i,fam in enumerate(product_families) if fam=='side'}
            side_candidates=[row for row in valid if row[1] in side_indices or row[1]>=len(product_candidates)]
            # A forced side-family trial must never silently fall back to a
            # trunk end: such a fallback falsifies the diagnostic comparison.
            route=(min(side_candidates,key=lambda x:tie_network_key(x[0],x[2]))[2]
                   if side_candidates else None)
        elif _product_route_mode=='trunk_nearest':
            nearest=[row for row in valid
                     if row[1]<len(product_candidates) and
                     product_families[row[1]] in ('trunk','end')
                     and len(row[2])>=2 and abs(row[2][0].u-row[2][1].u)<EPS
                     and any(s.medium==m and s.purpose in
                        ('Хребет','Перспективный хребет','Перспективное продление хребта')
                        and s.horizontal and abs(s.a.v-row[2][0].v)<EPS
                        and min(s.a.u,s.b.u)-EPS<=row[2][0].u<=max(s.a.u,s.b.u)+EPS
                        for s in product_trunk)]
            route=(min(nearest,key=lambda row:tie_network_key(row[0],row[2]))[2]
                   if nearest else None)
        elif valid and _product_route_mode is None:
            # Compare the whole physical route.  With equal actual network
            # cost and elbows, approach the chosen tie-in port first along
            # U, then descend toward the trunk as late as possible.
            route=min(valid,key=lambda x:tie_network_key(x[0],x[2]))[2]
        else:
            route=None
        # Врезка не имеет стороны выхода: маршрут заканчивается в одной
        # координатной точке, без повторного перебора четырёх псевдопортов.
        terminal_checks=[]
        if route is not None:
            # Запись реальных локальных кандидатов до последующего
            # сравнения ГОТОВЫХ сетей; выбранный здесь вариант может быть
            # заменён поздней оптимизацией (это явно указывается в UI).
            direct_count=len(direct_side_options)
            proposed=[]
            for cost,idx,nodes in sorted(valid,key=lambda row:(row[0],row[1]))[:25]:
                kind=('Обход от разрешённого бокового порта' if idx>=len(product_candidates)
                      else 'Свободный торец продуктового хребта' if product_families[idx]=='end'
                      else 'Боковая грань центральной плиты' if product_families[idx]=='side'
                      else 'Боковая врезка продуктового хребта на опоре')
                proposed.append({'kind':kind,'target':(nodes[0].u,nodes[0].v),
                                 'score':round(cost,5),'length':round(route_len(nodes),5),
                                 'bends':external_bends(nodes),'result':'допустим'})
            local_decisions[('external','oil_product')]={
                'source':'external_product_route', 'choice':(
                    'Тройник продуктового хребта' if any(
                        t.medium=='oil_product' and t.purpose in ('Хребет','Перспективный хребет','Перспективное продление хребта')
                        and distance_segment_to_point(t.a,t.b,route[0])<1e-5
                        for t in product_trunk) else 'Боковая грань плиты'),
                'choice_path':[(q.u,q.v) for q in route],
                'choice_cost':next((row[0] for row in valid if row[2]==route),evaluate_external(route,m,phase)),
                'alternatives':proposed,
                'terminal_approaches':terminal_checks,
                'rejected':{'Недопустимо по ограничениям сети':max(0,len(product_candidates)-len(valid))},
                'note':'Локальные оценки продуктового маршрута. Позже полный оптимизатор может перестроить продуктовую линию после расчёта пожарного вывода.',
            }
            for a,b in zip(route,route[1:]):
                add(segs,a,b,m,phase,'Продуктовый нефтепровод: от центра к врезке')
            branch_bends+=external_bends(route)
        else:
            # Не подменяем запретный торец аварийным запасным маршрутом.
            # Показываем невозможность при текущей геометрии явно.
            raise ValueError('Для продуктового нефтепровода к точке врезки не найдено '
                             'допустимого бокового подключения центральной плиты или '
                             'подключения к продуктовому хребту. Торцы плиты запрещены.')

    # Пожарный узел — ТОЧКА без четырёх искусственных направлений.
    # Допустимы только наружный торец центральной плиты ИЛИ дальний
    # свободный торец хребтовой трубы той же среды. Боковые подключения
    # и торец центральной плиты, занятый хребтом, ЗАПРЕЩЕНЫ.
    m=p.fire_medium
    endpoints=fire_ends(plate,lanes,segs,m)
    well_branches=[s for s in segs if
                   ('скв.' in s.purpose.casefold() or 'общая нитка' in s.purpose.casefold()
                    or 'общий коридор' in s.purpose.casefold()
                    or 'от центра к врезке' in s.purpose.casefold())]

    def shares_fire_corridor(a,b):
        # Пучок запрещён даже с трубами ДРУГОЙ среды.
        if a.horizontal!=b.horizontal:return False
        if a.horizontal:
            offset=abs(a.a.v-b.a.v)
            alo,ahi=sorted((a.a.u,a.b.u));blo,bhi=sorted((b.a.u,b.b.u))
        else:
            offset=abs(a.a.u-b.a.u)
            alo,ahi=sorted((a.a.v,a.b.v));blo,bhi=sorted((b.a.v,b.b.v))
        overlap=max(0.,min(ahi,bhi)-max(alo,blo))
        return (offset<max(1.0,p.lane_gap+(PIPE_OD[a.medium]+PIPE_OD[b.medium])/2+.15)-1e-5
                and overlap>3.0)

    def fire_route_valid(ending,route):
        if len(route)<2 or route[0]!=ending.point:return False
        if abs(route[1].v-ending.point.v)>EPS or \
           (route[1].u-ending.point.u)*ending.direction<=EPS:
            return False
        for a,b in zip(route,route[1:]):
            if not protected_line_ok(a,b,placements):return False
            if inner_trunk_parallel(a,b,trunk_inner_zone(lanes,sign,head_u,placements)):
                return False
            # Весь пожарный вывод должен быть за пределами центральной плиты.
            if abs(a.v-b.v)<EPS:
                if plate.v_min+EPS<a.v<plate.v_max-EPS and \
                   min(plate.u_max,max(a.u,b.u))-max(plate.u_min,min(a.u,b.u))>EPS:
                    return False
            elif abs(a.u-b.u)<EPS:
                if plate.u_min+EPS<a.u<plate.u_max-EPS and \
                   min(plate.v_max,max(a.v,b.v))-max(plate.v_min,min(a.v,b.v))>EPS:
                    return False
        fire_segments=[Segment(a,b,m,'active','Подключение пожарного узла')
                       for a,b in zip(route,route[1:])]
        if any(shares_fire_corridor(a,b) for a in fire_segments for b in well_branches):
            return False
        return True

    valid=[]; examined=0
    for ending in endpoints:
        for candidate in fire_candidate_paths(ending,fire):
            examined+=1
            if not fire_route_valid(ending,candidate):continue
            fire_segments=[Segment(a,b,m,'active','Подключение пожарного узла')
                           for a,b in zip(candidate,candidate[1:])]
            # Совпадение с чужой веткой своей среды не является тройником.
            # Пересечения РАЗНЫХ сред в плане допустимы как отдельные
            # обозначенные переходы, но не могут образовывать пучок/тройник.
            same_medium_crossings=sum(
                a.medium==b.medium and intersect(a,b)[0]=='cross'
                for a in fire_segments for b in well_branches)
            if same_medium_crossings:continue
            cost=evaluate_external(candidate,m,'active','Подключение пожарного узла')
            if not math.isfinite(cost):continue
            valid.append((route_length(candidate),route_bends(candidate),
                          cost,0 if ending.kind=='plate' else 1,ending,candidate))
    if not valid:
        raise ValueError('Не найден независимый пожарный трубопровод от наружного торца '
                         'центральной плиты или дальнего торца хребта с соблюдением '
                         'защитных зон и запрета пучков.')
    # «Смотря что ближе»: выбирается наименьшая длина ДОПУСТИМОГО пути;
    # при равенстве — меньше отводов и цена всей конфигурации.
    valid.sort(key=lambda row:(round(row[0],6),row[1],round(row[2],6),row[3]))
    chosen_length,chosen_bends,chosen_cost,_,chosen_end,chosen=valid[0]
    local_decisions[('external','fire')]={
        'source':'external_fire_route', 'choice':chosen_end.name,
        'choice_path':[(q.u,q.v) for q in chosen],
        'choice_cost':chosen_cost,
        'alternatives':[{'kind':e.name,'target':(e.point.u,e.point.v),
                         'score':round(cost,5),'length':round(length,5),
                         'bends':bends,'result':'допустим'}
                        for length,bends,cost,_,e,path in valid[:20]],
        'terminal_approaches':[],
        'rejected':{'Защитная зона, внутренняя полоса, общий коридор или столкновение':
                    max(0,examined-len(valid))},
        'note':'Пожарный узел — единая точка. Сравниваются два наружных торца; '
               'первый участок идёт по фантомному продолжению магистрали, '
               'второй поворачивает к узлу. Пожарный вывод не входит в пучки.',
    }
    for a,b in zip(chosen,chosen[1:]):
        add(segs,a,b,m,'active','Подключение пожарного узла')
    branch_bends+=chosen_bends

    # Инвариант интерфейса: боковые/хребтовые подключения специальных
    # сред не могут под видом торцевого продолжения попадать на плиту.
    # Проверяем до слияния сегментов, пока не потерялось назначение ветки.
    for segment in segs:
        if segment.medium not in SIDE_ONLY_PLATE_MEDIA:
            continue
        purpose=segment.purpose.casefold()
        if (segment.purpose==PLATE_PURPOSE or segment.purpose in
                ('Хребет','Перспективный хребет','Перспективное продление хребта') or
                'пожарного узла' in purpose):
            continue
        for face_u in (plate.outer_face_u,plate.trunk_face_u):
            port=Pt(face_u,lanes[segment.medium])
            if distance_segment_to_point(segment.a,segment.b,port) <= 1e-5:
                raise ValueError(f'Ошибка подключения {MEDIA_RU[segment.medium]}: '
                                 'посторонняя труба примкнула к торцу или пересекла '
                                 'торец центральной плиты по оси магистрали. '
                                 'Разрешены только боковой порт или тройник хребта.')

    # Сквозное правило потока: если ВСЕ источники среды перспективные,
    # ВСЕ её участки, включая потребителей, хребет, пожарный вывод и врезку, — перспектива.
    segs,whole_future_media=propagate_future_flow(segs,placements)
    # Плиты 3 × 1,75 м под хребтом: центры по фактическим точкам примыкания
    # отпайков ОСЕВЫХ скважин. Нитки, заканчивающиеся на такой плите,
    # продолжаются до её края, чтобы их конец образовывал тройник с заглушкой,
    # а не фиктивный отвод. Транзитные плиты не размещаем.
    from axial_plates import (
        create_axial_junction_plates,
        create_axial_branch_support_plates,
        create_intermediate_trunk_plates,
        create_endpoint_support_plates,
        create_transit_run_plates,
    )
    junction_slabs, trunk_extensions=create_axial_junction_plates(
        placements, branch_ports, segs, lanes, plate, MEDIA)
    segs.extend(trunk_extensions)
    # ЖЁСТКОЕ ограничение: в чужие 5-метровые зоны никакие линии не входят.
    inner_bad=inner_trunk_transit_conflicts(segs,placements,lanes,sign,head_u)
    if inner_bad:
        s=inner_bad[0]
        raise ValueError(f'Продольная транзитная линия {MEDIA_RU[s.medium]} '
                         'оказалась в защитной полосе между НДС и хребтом')
    violations=protected_well_conflicts(segs,placements)
    if violations:
        no,m,purpose=violations[0]
        raise ValueError(f'Недопустимая трасса {MEDIA_RU[m]} в 5-метровой зоне скважины №{no} ({purpose}).')
    # Удалить повторы частей одного трубопровода; перспектива отдельно.
    segs=merge_segments(segs)
    wrong_connections=forbidden_foreign_contacts(segs,head_u)
    stats={m:{'active':0.,'future':0.,'total':0.} for m in MEDIA}
    for s in segs:
        stats[s.medium][s.phase]+=s.length
        stats[s.medium]['total']+=s.length
    # Учитываем физическую длину продолжений ниток на новых осевых плитах.
    occupied_end=max([head_u]+[max(t for t in (a,b) if t is not None) for a,b in tails.values() if a is not None or b is not None])
    # Только реально используемые трубы на центральной плите.
    # Транзитные плиты пока НЕ специфицируются, их экономия условна.
    used_plate_media=tuple(m for m in MEDIA if any(s.medium==m and s.purpose==PLATE_PURPOSE for s in segs))
    slabs=[Slab(plate.u_min,plate.u_max,plate.v_min,plate.v_max,
                'active',len(used_plate_media),used_plate_media,'central')]
    slabs.extend(junction_slabs)
    branch_slabs=create_axial_branch_support_plates(placements,segs,plate_offset=p.branch_plate_offset)
    slabs.extend(branch_slabs)
    intermediate_slabs=create_intermediate_trunk_plates([slabs[0],*junction_slabs],segs,lanes,plate)
    slabs.extend(intermediate_slabs)
    endpoint_slabs=create_endpoint_support_plates(placements,segs,tie,fire)
    slabs.extend(endpoint_slabs)
    transit_run_slabs=create_transit_run_plates(segs, slabs)
    slabs.extend(transit_run_slabs)
    crossings=collision_notes(segs)
    notes=[
        'Торец центральной плиты у хребта закреплён на U = %.2f м; внеосевые скважины его не перемещают.' % head_u,
        'Порядок труб фиксирован: СППВ → ППД → замер нефти → газ → продуктовая нефть (зеркально при развороте).',
        'Хребет подключён к внутреннему торцу плиты. СППВ, ППД и продуктовая нефть (кроме пожарной линии): только боковая грань или хребет; независимая пожарная линия — от наружного торца плиты либо дальнего торца хребта. Замерная линия изолирована.',
        'Центральный узел: плита 6,00 × 1,75 м, только востребованные магистральные нитки длиной 6 м.',
        f'Осевые отпайки: {len(junction_slabs)} плит 3,00 × 1,75 м под хребтовыми нитками; в местах подключения — тройники.',
        f'Под отпаячными линиями: {len(branch_slabs)} плит 1,20 × 6,00 м с отступом {p.branch_plate_offset:.2f} м от скважины; одна плита может нести весь коридор скважины.',
        f'Промежуточные хребтовые плиты: {len(intermediate_slabs)} шт.; ставятся по центру между соседними соединительными плитами.',
        f'Локальные плиты 1,20 × 3,00 м: {len(endpoint_slabs)} шт.; полная длина сохраняется, у короткого последнего плеча поворот внутри плиты; соседние параллельные нитки накрываются одной плитой, если помещаются в 1,2 м.',
        f'Плиты транзитных линий и магистралей 1,20 × 3,00 м: {len(transit_run_slabs)} шт.; на каждом прямом участке раскладываются с равными промежутками 2..4 м и одинаковыми краевыми отступами.',
        'Арматура и опоры пока не моделируются.',
        'Внеосевые скважины подключаются к своей среде по тройникам; пожарная нитка независима и НЕ участвует в общих коридорах с любыми средами.',
        'Для условных блоков пересечений хранится приоритет: какая труба считается верхней и какая нижней.',
        'При сокращении суммарной стоимости разрешена врезка ответвления в уже соединённую с центром трубу той же среды; тупиковые участки удаляются.',
        'Запретная зона вокруг любой скважины: радиус 5 м; чужие трубы прокладываются в обход.',
        'От скважин НЕТ отдельных отводов к замерному нефтепроводу; подключения появятся после проработки узлов.',
        
        'К точке врезки выходит только продуктовый нефтепровод; авто-врезка расположена на перпендикуляре через центр.',
        'Пожарный вывод сравнивает оба разрешённых торца по длине допустимого маршрута и продолжает магистраль по фантомной оси перед поворотом.',
        'Замерной нефтепровод изолирован от внешних линий; точка врезки подключена только к порту продуктовой нефти.',
        'Повороты только на 90° в локальных осях U/V, горизонтальных отметок и обхода препятствий нет.',
        'Замерной и продуктовый нефтепроводы малиновые, но рассчитываются отдельно.',
        'Подбор диаметров по справочнику Excel — ориентировочный; арматура не учитывается.',
    ]
    notes.extend(tap_notes)
    if crossings:notes.append(f'Найдены пересечения/наложения трубных осей: {len(crossings)}. Наложения имеют жёсткий штраф 10000; оставшиеся коллизии требуют проверки инженером.')
    if wrong_connections:
        notes.append(f'ОШИБКА ГЕОМЕТРИИ: {len(wrong_connections)} касаний торца с трубой другой среды. '
                     'Это НЕ тройники и НЕ корректно оформленные пересечения!')
        notes.extend(wrong_connections[:8])
    from network_cost import network_fittings, transit_corridor_overlap, estimate_score
    fitting=network_fittings(segs)
    metrics={'axial_plate_count':float(len(junction_slabs)),
             'axial_branch_plate_count':float(len(branch_slabs)),
             'intermediate_trunk_plate_count':float(len(intermediate_slabs)),
             'endpoint_plate_count':float(len(endpoint_slabs)),
             'transit_run_plate_count':float(len(transit_run_slabs)),
             'central_plate_count':1.0,
             'pipe_m':sum(v['total'] for v in stats.values()),
             'shared_corridor_m':shared_corridor_length(segs,max(.9, p.lane_gap+.4)),
             'transit_corridor_m':transit_corridor_overlap(segs,p.lane_gap),
             'topological_elbows':float(fitting['elbows']),
             'topological_tees':float(fitting['tees']),
             'fourway_count':float(len(fitting['fourways'])),
             'group_count':float(len(bundles)),
             'small_plate_count':float(__import__('network_cost').count_small_plates(slabs)),
             'bends':float(branch_bends),
             'crossings':float(len(crossings)),
             'overlaps':float(sum('Наложение осей' in label for label in crossings)),
             'guard_violations':0.0,
             'foreign_contacts':float(len(wrong_connections))}
    # Использовать одинаковую целевую функцию в автоматическом выборе 4 положений и оптимизаторах.
    simple=type('CostDraft',(),{'project':p,'metrics':metrics,'slabs':slabs})()
    metrics['optimized_score']=estimate_score(simple)
    from network_cost import axial_branch_conflicts
    axial_flags=axial_branch_conflicts(segs,[wp.well.number for wp in placements if wp.well.on_axis])
    metrics['axial_branch_crossings']=float(len(axial_flags))
    if axial_flags:
        notes.append(f'ВНИМАНИЕ: {len(axial_flags)} пересечений осевых отпайков с нехребтовыми линиями. '
                     'Пересечения с хребтом разрешены: выше обозначается отпаечная нитка. '
                     'Для прочих пересечений требуется проверка компоновки.')
    if whole_future_media:
        notes.append('Вся сеть перспективная для сред: '+', '.join(MEDIA_RU[m] for m in sorted(whole_future_media,key=MEDIA.index))+'. Причина — все источники потока перспективные.')
    if bundles:
        notes.append('Общие коридоры внеосевых скважин: '+', '.join('/'.join(map(str,g)) for g in bundles)+'.')
    notes.append('v75: раздельные линии и допустимые пучки сравниваются по рублёвой J: трубы + отводы + плиты 1,2×3 м; тройники — по отдельной редактируемой ставке. Нет скидки за визуальную близость ниток.')
    notes.append(f'Фасонные узлы: отводов {fitting["elbows"]}, тройников {fitting["tees"]} (из них торцевых хребтовых {len(fitting["terminal_tees"])}).')
    notes.append('Плиты транзитных линий и магистралей рассчитываются как повторяющиеся плиты 1,20 × 3,00 м; шаг между соседними плитами одинаков на каждом прямом свободном интервале и лежит в диапазоне 2..4 м.')
    notes.append('Промежуточные хребтовые плиты: gap < 5 м — не ставятся; 5..11 м — 1,75 × 3 м; > 11 м — 1,75 × 6 м.')
    notes.append('Плиты 1,20 × 3,00 м считаются физически: одна плита может поддерживать несколько самостоятельных ниток, только если она действительно покрывает их габариты.')
    notes.append('Зазор между наружными поверхностями соседних труб составляет %.2f м (газовый диаметр предварительный).' % p.lane_gap)
    notes.append('Не учитываются рельеф, препятствия, нагрузки, гидравлика и нормативные расстояния. Оптимум — локальный среди проверенных вариантов.')
    result=Result(p,frame,placements,center,tie,fire,segs,slabs,lanes,stats,crossings,notes,head_u,occupied_end,tie_ports,metrics,bundles,sign,central_plate=plate,decision_trace=local_decisions)
    product_for_diagnostics=[s for s in segs if s.purpose=='Продуктовый нефтепровод: от центра к врезке']
    if product_for_diagnostics:
        selected_length=sum(s.length for s in product_for_diagnostics)
        side_lower_bound=min((abs(u-tie.u)+abs(lanes['oil_product']-tie.v)
                              for u in (plate.u_min+0.35,plate.center_u,plate.u_max-0.35)),
                             default=selected_length)
        result.metrics['tie_pipe_m']=selected_length
        result.metrics['tie_side_lower_bound_m']=side_lower_bound
        if selected_length>side_lower_bound+6.0:
            result.notes.append(f'ДИАГНОСТИКА ВРЕЗКИ: маршрут {selected_length:.2f} м; '
                                f'геометрический минимум бокового подхода {side_lower_bound:.2f} м. '
                                'Проверяется альтернативное боковое подключение; '
                                'если оно недопустимо, причина — ограничения сети и охранные зоны.')
    # Very long product routes can be locally preferred before the FIRE
    # routing exists, yet become more expensive after the independent fire
    # line is added. Evaluate ONE alternative complete network, not merely
    # a different pipe length, and retain the lower full-network J.
    if _product_route_mode is None and _overrides is not None:
        product=[s for s in segs if s.purpose=='Продуктовый нефтепровод: от центра к врезке']
        if product:
            actual=sum(s.length for s in product)
            side_min=min((abs(u-tie.u)+abs(lanes['oil_product']-tie.v)
                          for u in (plate.u_min+0.35,plate.center_u,plate.u_max-0.35)),default=actual)
            if actual > side_min + max(6.0,1.5*p.elbow_cost/max(p.pipe_cost_per_m,0.001)):
                try:
                    alternative=build(p,_overrides=_overrides,_bundles=_bundles,_side=_side,
                                      _orientations=_orientations,_taps=_taps,
                                      _product_route_mode='side_safe')
                    if ((alternative.metrics.get('foreign_contacts',0),alternative.metrics['optimized_score'])
                         < (result.metrics.get('foreign_contacts',0),result.metrics['optimized_score']-1e-6)):
                        alternative.notes.append('Длинное подключение продуктовой врезки заменено '
                                                 'на более дешёвый маршрут после оценки ПОЛНОЙ сети, '
                                                 'включая пожарное подключение.')
                        return alternative
                except ValueError:
                    pass
    # При полной компоновке пожарный вывод и переиспользуемые сегменты
    # могут изменить рейтинг двух продуктовых кандидатов. Вместо локальной
    # надбавки за геометрию сравниваем ГОТОВУЮ сеть с ближайшим тройником
    # на продуктовом хребте против ранее выбранного маршрута.
    if _product_route_mode is None and _overrides is not None:
        product=[s for s in result.segments
                 if s.purpose=='Продуктовый нефтепровод: от центра к врезке']
        trunk=[s for s in result.segments if s.medium=='oil_product'
               and s.purpose in ('Хребет','Перспективный хребет',
                                 'Перспективное продление хребта')]
        already_direct=any(s.length>1 and not s.horizontal
                           and abs(s.a.u-tie.u)<.05 and abs(s.b.u-tie.u)<.05
                           for s in product)
        selected_len=sum(s.length for s in product)
        closest_geom=min((abs(tie.u-max(min(t.a.u,t.b.u),min(max(t.a.u,t.b.u),tie.u)))
                          +abs(tie.v-t.a.v) for t in trunk),default=float('inf'))
        # Дорогую повторную сборку выполняем только при подозрительном
        # обходе: достаточно длинном относительно геометрического минимума
        # до реально существующей нитки хребта.
        if (trunk and product and not already_direct and
            selected_len > closest_geom+max(2.0,
                              p.elbow_cost/max(p.pipe_cost_per_m,0.001))):
            try:
                alternative=build(p,_overrides=_overrides,_bundles=_bundles,_side=_side,
                                  _orientations=_orientations,_taps=_taps,
                                  _product_route_mode='trunk_nearest')
                old_rank=(result.metrics.get('foreign_contacts',0),
                          result.metrics.get('fourway_count',0),
                          result.metrics['optimized_score'])
                new_rank=(alternative.metrics.get('foreign_contacts',0),
                          alternative.metrics.get('fourway_count',0),
                          alternative.metrics['optimized_score'])
                if new_rank < (old_rank[0],old_rank[1],old_rank[2]-1e-6):
                    alternative.notes.append('К точке врезки выбрано допустимое '
                                             'соединение с продуктовым хребтом после сравнения '
                                             'полной стоимости обеих сетей.')
                    return alternative
            except ValueError:
                pass
    return result



def export_csv(r:Result,path):
    with open(path,'w',newline='',encoding='utf-8-sig') as stream:
        w=csv.writer(stream,delimiter=';')
        w.writerow(['Среда','Стадия','Назначение','X начала','Y начала','X конца','Y конца','Длина, м'])
        for s in r.segments:
            x1,y1=r.frame.xy(s.a);x2,y2=r.frame.xy(s.b)
            w.writerow([MEDIA_RU[s.medium],STATUS_RU[s.phase],s.purpose,round(x1,3),round(y1,3),round(x2,3),round(y2,3),round(s.length,3)])
        w.writerow([])
        analysis=r.crossing_markup
        if analysis is not None:
            assigned={ix: mark for mark in analysis.markers for ix in mark.indices}
            w.writerow(['УСЛОВНЫЕ УЧАСТКИ ПЕРЕСЕЧЕНИЙ — БЕЗ РАСЧЁТА ВЫСОТЫ'])
            w.writerow(['№ пересечения','Среда А','Среда Б','X','Y','Участок','Отмеченная среда','Пересечений в участке'])
            for ix,c in enumerate(analysis.crossings):
                x,y=r.frame.xy(c.point)
                mark=assigned.get(ix)
                w.writerow([ix+1,MEDIA_RU[analysis.runs[c.a].medium],
                            MEDIA_RU[analysis.runs[c.b].medium],round(x,3),round(y,3),
                            'У'+str(mark.number) if mark else 'Не размечено',
                            MEDIA_RU[analysis.runs[mark.run_id].medium] if mark else '',
                            len(mark.indices) if mark else 0])
            w.writerow([])
        w.writerow(['Оценка совместной прокладки (не расчёт плит)'])
        w.writerow(['Длина всех труб, м',round(r.metrics.get('pipe_m',0),3)])
        w.writerow(['Условная длина общих коридоров, м',round(r.metrics.get('shared_corridor_m',0),3)])
        if 'original_corridor_m' in r.metrics:
            w.writerow(['Коридоры без объединения, м',round(r.metrics['original_corridor_m'],3)])
            w.writerow(['Трубы без объединения, м',round(r.metrics['original_pipe_m'],3)])
        w.writerow(['Объединённых групп внеосевых скважин',len(r.routing_groups)])
        w.writerow(['Число отводов 90 градусов (предварительно)',int(r.metrics.get('bends',0))])
        w.writerow(['Число коллизий',int(r.metrics.get('crossings',0))])
        w.writerow(['Целевая функция J, руб.',round(score(r),2)])
        from network_cost import cost_components, count_small_plates
        components=cost_components(r)
        for title,key in [('Стоимость труб, руб.','pipe'),('Стоимость отводов, руб.','elbows'),('Стоимость плит 1,2×3 м, руб.','small_plates'),('Стоимость тройников, руб.','tees')]:
            w.writerow([title,round(components[key],2)])
        w.writerow(['Количество плит 1,2×3 м, шт.',count_small_plates(r.slabs)])
        w.writerow(['Ставка за метр трубы, руб.',r.project.pipe_cost_per_m])
        w.writerow(['Ставка за отвод, руб.',r.project.elbow_cost])
        w.writerow(['Ставка за малую плиту, руб.',r.project.plate_cost])
        if 'original_score' in r.metrics:w.writerow(['J исходной независимой конфигурации, руб.',round(r.metrics['original_score'],2)])
        if r.central_plate is not None:
            plate=r.central_plate
            w.writerow([])
            w.writerow(['ЦЕНТРАЛЬНАЯ ПЛИТА — площадь и магистральные нитки'])
            w.writerow(['Длина плиты, м',round(plate.u_max-plate.u_min,3)])
            w.writerow(['Ширина плиты, м',round(plate.v_max-plate.v_min,3)])
            used_central=next((s.media for s in r.slabs if s.slab_type=='central'), ())
            w.writerow(['Количество используемых магистралей',len(used_central)])
            w.writerow(['Торец примыкания хребта U, м',round(plate.trunk_face_u,3)])
            w.writerow(['Свободный торец U, м',round(plate.outer_face_u,3)])
            for m in used_central:
                w.writerow(['Магистраль '+MEDIA_RU[m]+'; ось V, м',round(r.lanes[m],3)])
        w.writerow([])
        w.writerow(['ПЛИТЫ ОСЕВЫХ ПОДКЛЮЧЕНИЙ К ХРЕБТУ — 3,00 × 1,75 м'])
        w.writerow(['№','X центра, м','Y центра, м','Длина, м','Ширина, м','Состояние','Среды'])
        axial=[s for s in r.slabs if s.slab_type=='axial_junction']
        for i,slab in enumerate(axial,1):
            x,y=r.frame.xy(Pt(slab.u,(slab.v_min+slab.v_max)/2))
            w.writerow([i,round(x,3),round(y,3),round(slab.length,3),
                        round(slab.height,3),STATUS_RU[slab.phase],
                        ', '.join(MEDIA_RU[m] for m in slab.media)])
        w.writerow(['Итого плит осевых подключений',len(axial)])
        w.writerow([])
        w.writerow(['ПЛИТЫ ПОД ОТПАЯЧНЫМИ ЛИНИЯМИ — 1,20 × 6,00 м'])
        w.writerow(['№','X центра, м','Y центра, м','Длина по U, м','Ширина по V, м','Состояние','Среда'])
        branch=[s for s in r.slabs if s.slab_type=='axial_branch']
        for i,slab in enumerate(branch,1):
            x,y=r.frame.xy(Pt(slab.u,(slab.v_min+slab.v_max)/2))
            w.writerow([i,round(x,3),round(y,3),round(slab.length,3),
                        round(slab.height,3),STATUS_RU[slab.phase],
                        ', '.join(MEDIA_RU[m] for m in slab.media)])
        w.writerow(['Итого плит под отпайками',len(branch)])
        w.writerow([])
        w.writerow(['ПРОМЕЖУТОЧНЫЕ ХРЕБТОВЫЕ ПЛИТЫ'])
        w.writerow(['№','X центра, м','Y центра, м','Длина, м','Ширина, м','Состояние','Среды'])
        trunk_mid=[s for s in r.slabs if s.slab_type=='trunk_intermediate']
        for i,slab in enumerate(trunk_mid,1):
            x,y=r.frame.xy(Pt(slab.u,(slab.v_min+slab.v_max)/2))
            w.writerow([i,round(x,3),round(y,3),round(slab.length,3),
                        round(slab.height,3),STATUS_RU[slab.phase],
                        ', '.join(MEDIA_RU[m] for m in slab.media)])
        w.writerow(['Итого промежуточных хребтовых плит',len(trunk_mid)])
        w.writerow([])
        w.writerow(['ЛОКАЛЬНЫЕ ПЛИТЫ 1,20 × 3,00 м'])
        w.writerow(['№','X центра, м','Y центра, м','Длина, м','Ширина, м','Тип','Состояние','Среды'])
        local=[s for s in r.slabs if s.slab_type in ('terminal_node','offaxis_branch')]
        type_names={'terminal_node':'точка врезки / пожарный узел','offaxis_branch':'внеосевая скважина'}
        for i,slab in enumerate(local,1):
            x,y=r.frame.xy(Pt(slab.u,(slab.v_min+slab.v_max)/2))
            w.writerow([i,round(x,3),round(y,3),round(slab.length,3),
                        round(slab.height,3),type_names.get(slab.slab_type,slab.slab_type),
                        STATUS_RU[slab.phase],', '.join(MEDIA_RU[m] for m in slab.media)])
        w.writerow(['Итого локальных плит',len(local)])
        w.writerow([])
        w.writerow(['СОЕДИНИТЕЛЬНЫЕ ХРЕБТОВЫЕ ПЛИТЫ ПОД ТРАНЗИТНЫМИ УЗЛАМИ — 3,00 × 1,75 м'])
        w.writerow(['№','X центра, м','Y центра, м','Длина, м','Ширина, м','Состояние','Среды'])
        added_trunk=[s for s in r.slabs if s.slab_type=='trunk_tap_plate']
        for i,slab in enumerate(added_trunk,1):
            x,y=r.frame.xy(Pt(slab.u,(slab.v_min+slab.v_max)/2))
            w.writerow([i,round(x,3),round(y,3),round(slab.length,3),round(slab.height,3),
                        STATUS_RU[slab.phase],', '.join(MEDIA_RU[m] for m in slab.media)])
        w.writerow(['Итого соединительных хребтовых плит',len(added_trunk)])
        w.writerow([])
        w.writerow(['ПЛИТЫ ТРАНЗИТНЫХ ЛИНИЙ И МАГИСТРАЛЕЙ — 1,20 × 3,00 м'])
        w.writerow(['№','X центра, м','Y центра, м','Длина, м','Ширина, м','Состояние','Среды'])
        transit=[s for s in r.slabs if s.slab_type=='transit_run']
        for i,slab in enumerate(transit,1):
            x,y=r.frame.xy(Pt(slab.u,(slab.v_min+slab.v_max)/2))
            w.writerow([i,round(x,3),round(y,3),round(slab.length,3),
                        round(slab.height,3),STATUS_RU[slab.phase],
                        ', '.join(MEDIA_RU[m] for m in slab.media)])
        w.writerow(['Итого транзитных плит',len(transit)])
        w.writerow([])
        w.writerow(['ОПОРНЫЕ ПЛИТЫ ФАСОННЫХ УЗЛОВ — 1,20 × 3,00 м; 0,25 м'])
        w.writerow(['№','X центра, м','Y центра, м','Длина, м','Ширина, м','Состояние','Среды'])
        supports=[s for s in r.slabs if s.slab_type=='fitting_support']
        for i,slab in enumerate(supports,1):
            x,y=r.frame.xy(Pt(slab.u,(slab.v_min+slab.v_max)/2))
            w.writerow([i,round(x,3),round(y,3),round(slab.length,3),
                        round(slab.height,3),STATUS_RU[slab.phase],
                        ', '.join(MEDIA_RU[m] for m in slab.media)])
        w.writerow(['Итого опорных плит',len(supports)])
        w.writerow(['Узлов для проверки',int(r.metrics.get('fitting_site_count',0))])
        w.writerow(['Узлов без опоры',int(r.metrics.get('fitting_unsupported_count',0))])
        w.writerow([]);w.writerow(['Проверки и замечания'])
        for t in r.notes+r.crossings:w.writerow([t])
