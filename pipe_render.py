"""Единая схема отрисовки реальных условных контуров труб для Tkinter и SVG.

Вход: ортогональные осевые отрезки расчётного ядра. Выход: геометрические
примитивы в экранных координатах. Расчётные длины/маршруты не меняются.
Колено показывается закруглением; тройник ставится лишь в узле одной среды,
где ветка действительно подключена к уже существующей нитке.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
import math
from routing import PIPE_OD, MEDIA_COLOR

_EPS=1e-6

def key(pt):
    return (round(pt[0], 6), round(pt[1], 6))

def _on_edge(p,a,b):
    if abs(a[1]-b[1])<_EPS:
        return (abs(p[1]-a[1])<_EPS and min(a[0],b[0])-_EPS<=p[0]<=max(a[0],b[0])+_EPS)
    if abs(a[0]-b[0])<_EPS:
        return (abs(p[0]-a[0])<_EPS and min(a[1],b[1])-_EPS<=p[1]<=max(a[1],b[1])+_EPS)
    return False

def make_network(segments):
    """Объединить соосные повторы и разделить линии в существующих T-узлах.

    Возвращает {(среда,): [(a,b,phase)]}, a/b — координаты модели.
    Будущая часть не перекрывает действующую там, где обе совпадают.
    """
    raw=defaultdict(list)
    for s in segments:
        a=key((s.a.u,s.a.v));b=key((s.b.u,s.b.v))
        if a!=b:
            raw[s.medium].append((a,b,s.phase))
    networks={}
    for medium,lines in raw.items():
        endpoints=set()
        for a,b,_ in lines:
            endpoints.update((a,b))
        rows=defaultdict(list);columns=defaultdict(list)
        for p in endpoints:
            rows[p[1]].append(p)
            columns[p[0]].append(p)
        unique={}
        for a,b,phase in lines:
            candidates=rows[a[1]] if abs(a[1]-b[1])<_EPS else columns[a[0]]
            cuts=[p for p in candidates if _on_edge(p,a,b)]
            if abs(a[1]-b[1])<_EPS:
                cuts.sort(key=lambda x:x[0])
            else:
                cuts.sort(key=lambda x:x[1])
            for p,q in zip(cuts,cuts[1:]):
                if p==q:continue
                edge=(p,q) if p<=q else (q,p)
                existing=unique.get(edge)
                if existing is None or phase=='active':
                    unique[edge]=phase
        networks[medium]=[(a,b,phase) for (a,b),phase in sorted(unique.items())]
    return networks

@dataclass(frozen=True)
class Shape:
    kind:str  # line, polygon, ellipse
    points:tuple
    stroke:str
    width:float=1.0
    fill:str|None=None
    dash:str|None=None


def _line(a,b,color,width=1,dash=None):
    return Shape('line',(a,b),color,width,None,dash)

def _poly(points,color,width=1,fill=None,dash=None):
    return Shape('polygon',tuple(points),color,width,fill,dash)


def _length(a,b):
    return math.hypot(a[0]-b[0],a[1]-b[1])

def _unit(a,b):
    z=max(_length(a,b),1e-12)
    return ((b[0]-a[0])/z,(b[1]-a[1])/z)

def _offset(p,n,d):return (p[0]+n[0]*d,p[1]+n[1]*d)

def _normal(d):return (-d[1],d[0])

def _quadratic(p0,p1,p2,t):
    v=1-t
    return (v*v*p0[0]+2*t*v*p1[0]+t*t*p2[0],
            v*v*p0[1]+2*t*v*p1[1]+t*t*p2[1])

def _quadratic_tangent(p0,p1,p2,t):
    d=(2*(1-t)*(p1[0]-p0[0])+2*t*(p2[0]-p1[0]),
       2*(1-t)*(p1[1]-p0[1])+2*t*(p2[1]-p1[1]))
    z=max(math.hypot(*d),1e-12)
    return (d[0]/z,d[1]/z)

def pipe_width(outer_diameter_m, pixels_per_meter):
    """Схемная видимая толщина: пропорциональна OD, но не тоньше 4.8 px.

    Для крупных планов реальные миллиметры неразличимы на экране,
    поэтому включена минимальная графическая толщина.
    """
    return max(4.8,min(22.0,outer_diameter_m*pixels_per_meter))


def primitives(segments, xy, pixels_per_meter, show_axis=True, background="#ffffff"):
    """Сформировать контуры, колена 90°, тройники, открытые торцы и оси.

    xy((u,v)) -> (x,y) — единое преобразование для Tk и SVG.
    """
    networks=make_network(segments)
    trunk_purposes={'Хребет','Перспективный хребет','Перспективное продление хребта'}
    trunk_ends={(s.medium,key((q.u,q.v))) for s in segments if s.purpose in trunk_purposes for q in (s.a,s.b)}
    foreground=[]
    centerlines=[]
    for medium,edges in networks.items():
        if not edges:continue
        color=MEDIA_COLOR[medium]
        width=pipe_width(PIPE_OD[medium],pixels_per_meter)
        h=width/2
        graph=defaultdict(set)
        edge_phase={}
        for a,b,phase in edges:
            graph[a].add(b);graph[b].add(a)
            edge_phase[(a,b)]=phase
        pixel={p:tuple(map(float,xy(p))) for p in graph}
        corners={}
        for node,neighbors in graph.items():
            if len(neighbors)!=2:continue
            aa,bb=list(neighbors)
            da=_unit(pixel[node],pixel[aa]);db=_unit(pixel[node],pixel[bb])
            # Колено только там, где направление меняется на прямой угол.
            if abs(da[0]*db[0]+da[1]*db[1])>0.02:continue
            # В конце хребта ответвление образует тройник с заглушкой,
            # а не отвод, даже когда на плане всего два сегмента.
            if (medium,node) in trunk_ends:continue
            r=min(max(9.0,width*1.8), _length(pixel[node],pixel[aa])*.36,
                  _length(pixel[node],pixel[bb])*.36)
            if r>1.0:corners[node]=r
        # 1. Контур прямых участков, подрезанных на коленах.
        for a,b,phase in edges:
            pa,pb=pixel[a],pixel[b]
            d=_unit(pa,pb);n=_normal(d)
            sta=_offset(pa,d,corners.get(a,0.0))
            end=_offset(pb,d,-corners.get(b,0.0))
            dash='7 5' if phase=='future' else None
            for sign in (-1,1):
                foreground.append(_line(_offset(sta,n,sign*h),_offset(end,n,sign*h),color,1.8,dash))
            foreground.append(_line(sta,end,'#ffffff',max(1.2,width-3.6),dash))
            if show_axis:
                centerlines.append(_line(sta,end,'#171717',1.1,'8 3 1 3'))
        # 2. Закруглённые отводы в узлах из двух ортогональных рёбер.
        for node,r in corners.items():
            neighbors=list(graph[node]);j=pixel[node]
            va=_unit(j,pixel[neighbors[0]]);vb=_unit(j,pixel[neighbors[1]])
            start=_offset(j,va,r);end=_offset(j,vb,r)
            # Кривая p0 -> j -> p2 с гладкими сопряжениями.
            path=[];paths=[[],[]]
            for i in range(17):
                t=i/16
                pt=_quadratic(start,j,end,t)
                tangent=_quadratic_tangent(start,j,end,t)
                nn=_normal(tangent)
                path.append(pt)
                for jx,sign in enumerate((-1,1)):
                    paths[jx].append(_offset(pt,nn,sign*h))
            future=all(edge_phase.get(tuple(sorted((node,neighbor))), 'active')=='future' for neighbor in neighbors)
            dash='7 5' if future else None
            # Самостоятельная фасонная деталь: замкнутый цветной контур,
            # заливка как фон чертежа и два поперечных стыка на концах.
            # Тройник оформляется аналогично. Оси чертятся поверх детали.
            elbow_outline = tuple(paths[0] + list(reversed(paths[1])))
            foreground.append(_poly(elbow_outline,color,1.8,'#ffffff',dash))
            if show_axis:
                for aa,bb in zip(path,path[1:]):centerlines.append(_line(aa,bb,'#171717',1.1,'8 3 1 3'))
        # 3. Тройники: соединение именно одной среды, а не пересечение сред.
        for node,neighbors in graph.items():
            if len(neighbors)<3:continue
            j=pixel[node]
            dirs=[_unit(j,pixel[n]) for n in neighbors]
            # Горизонтальный/вертикальный основной проход определяется по паре противоположных векторов.
            pair=None
            for i in range(len(dirs)):
                for z in range(i+1,len(dirs)):
                    if dirs[i][0]*dirs[z][0]+dirs[i][1]*dirs[z][1]<-0.98:
                        pair=(i,z);break
                if pair:break
            if pair is None:continue
            main=dirs[pair[0]]
            side=_normal(main)
            branches=[dirs[i] for i in range(len(dirs)) if i not in pair]
            branch=branches[0] if branches else side
            if main[0]*side[1]-main[1]*side[0] and (side[0]*branch[0]+side[1]*branch[1])<0:
                side=(-side[0],-side[1])
            size=max(7.0,width*1.6)
            t_half=max(width*.68,3.5)
            # Фасонная деталь в форме Т. При крестовине показываем квадратную муфту.
            if len(neighbors)==3:
                local=[(-size,-h),(size,-h),(size,h),(t_half,h),
                       (t_half,size),(-t_half,size),(-t_half,h),(-size,h)]
            else:
                local=[(-size,-h),(-t_half,-h),(-t_half,-size),(t_half,-size),
                       (t_half,-h),(size,-h),(size,h),(t_half,h),
                       (t_half,size),(-t_half,size),(-t_half,h),(-size,h)]
            points=[(j[0]+main[0]*u+side[0]*v,j[1]+main[1]*u+side[1]*v) for u,v in local]
            future=all(edge_phase.get(tuple(sorted((node,n))), 'active')=='future' for n in neighbors)
            foreground.append(_poly(points,color,1.8,'#ffffff','7 5' if future else None))
        # 4. Тройник с заглушённым прямым продолжением на конце хребта.
        # В графе это 2 перпендикулярных ребра, но конструктивно здесь
        # тройник, а не колено. Рисуем отдельный корпус и заглушку.
        for node,neighbors in graph.items():
            if len(neighbors)!=2 or (medium,node) not in trunk_ends:continue
            j=pixel[node]
            nbs=list(neighbors)
            dirs=[_unit(j,pixel[n]) for n in nbs]
            if abs(dirs[0][0]*dirs[1][0]+dirs[0][1]*dirs[1][1])>0.02:continue
            hdir=next((d for d in dirs if abs(d[0])>0.8),None)
            vdir=next((d for d in dirs if abs(d[1])>0.8),None)
            if hdir is None or vdir is None:continue
            size=max(7.0,width*1.6)
            t_half=max(width*.68,3.5)
            local=[(-size,-h),(size,-h),(size,h),(t_half,h),
                   (t_half,size),(-t_half,size),(-t_half,h),(-size,h)]
            points=[(j[0]+hdir[0]*u+vdir[0]*v,j[1]+hdir[1]*u+vdir[1]*v)
                    for u,v in local]
            future=all(edge_phase.get(tuple(sorted((node,n))), 'active')=='future' for n in neighbors)
            foreground.append(_poly(points,color,1.8,'#ffffff','7 5' if future else None))
            ca=(j[0]-hdir[0]*size-vdir[0]*h,j[1]-hdir[1]*size-vdir[1]*h)
            cb=(j[0]-hdir[0]*size+vdir[0]*h,j[1]-hdir[1]*size+vdir[1]*h)
            foreground.append(_line(ca,cb,'#263647',2.6,'7 5' if future else None))
        # 5. Концевые открытые торцы.
        for node,neighbors in graph.items():
            if len(neighbors)!=1:continue
            j=pixel[node];direction=_unit(j,pixel[next(iter(neighbors))]);n=_normal(direction)
            if abs(direction[0])>=abs(direction[1]):
                rx,ry=max(2.3,h*.48),h
            else:
                rx,ry=h,max(2.3,h*.48)
            if (medium,node) in trunk_ends:
                # Концевая заглушка хребта, а не условный открытый торец.
                n=_normal(direction)
                ca=_offset(j,n,-h)
                cb=_offset(j,n,h)
                phase=edge_phase.get(tuple(sorted((node,next(iter(neighbors))))),'active')
                foreground.append(_line(ca,cb,'#263647',3.2,'7 5' if phase=='future' else None))
            else:
                phase=edge_phase.get(tuple(sorted((node,next(iter(neighbors))))),'active')
                foreground.append(Shape('ellipse',(j,(rx,ry)),color,1.6,'#ffffff',
                                        '7 5' if phase=='future' else None))
    # Оси рисуем после контуров и деталей, чтобы они были различимы.
    return foreground+centerlines
