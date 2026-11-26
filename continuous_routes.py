"""Непрерывные трассы трубопроводов поверх графа физических соединений.

Участки из routing.Segment остаются отдельными объектами для чертежа и
спецификации, но отвод не обрывает маршрут и смена назначения не разрывает
электронную сеть. Рёбра разных сред НЕ соединяются; ортогональные оси
без физического ответвления (без вершины/торца) НЕ создают фиктивный тройник.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import heapq
import math

TOL=1e-5


def _k(pt):
    if hasattr(pt,'u'):
        return round(pt.u,6),round(pt.v,6)
    return round(pt[0],6),round(pt[1],6)


def _on_edge(point, a, b):
    x,y=point
    if abs(a[1]-b[1])<TOL:
        return abs(y-a[1])<TOL and min(a[0],b[0])-TOL<=x<=max(a[0],b[0])+TOL
    if abs(a[0]-b[0])<TOL:
        return abs(x-a[0])<TOL and min(a[1],b[1])-TOL<=y<=max(a[1],b[1])+TOL
    return False


@dataclass(frozen=True)
class ContinuousPath:
    points:tuple[tuple[float,float],...]
    length:float
    elbows:int


class ContinuousNetwork:
    """Граф с рёбрами из всех геометрических частей выбранной среды."""
    def __init__(self, segments, medium, *, active_only=False, extra_points=()):
        from pipe_render import make_network
        from routing import Pt, Segment
        self.medium=medium
        self.segments=[s for s in segments if s.medium==medium and
                       (not active_only or s.phase=='active')]
        helper=list(self.segments)
        # Вставка вспомогательной точки разбивает реальное ребро на два,
        # но никогда не соединяет не имеющие физического узла пересечения.
        if extra_points:
            for pt in map(_k,extra_points):
                touching=[s for s in self.segments
                          if _on_edge(pt,_k(s.a),_k(s.b))]
                # Два проходящих насквозь взаимно перпендикулярных участка
                # без единого торца в точке пересечения НЕ соединяются.
                through_cross=(len({s.horizontal for s in touching})>1 and
                   not any(_k(s.a)==pt or _k(s.b)==pt for s in touching))
                if through_cross:
                    continue
                for s in self.segments:
                    a,b=_k(s.a),_k(s.b)
                    if pt==a or pt==b or not _on_edge(pt,a,b):
                        continue
                    helper.append(Segment(s.a,Pt(*pt),s.medium,s.phase,s.purpose))
                    helper.append(Segment(Pt(*pt),s.b,s.medium,s.phase,s.purpose))
        graph=defaultdict(dict)
        for a,b,_ in make_network(helper).get(medium,()):
            weight=abs(a[0]-b[0])+abs(a[1]-b[1])
            if weight<TOL:continue
            graph[a][b]=weight
            graph[b][a]=weight
        self.graph=dict(graph)

    def contains_point(self,point):
        pt=_k(point)
        return pt in self.graph or any(_on_edge(pt,_k(s.a),_k(s.b)) for s in self.segments)

    def component(self, point):
        start=_k(point)
        if start not in self.graph:return set()
        seen={start};todo=[start]
        while todo:
            for nxt in self.graph.get(todo.pop(),{}):
                if nxt not in seen:seen.add(nxt);todo.append(nxt)
        return seen

    def connected(self,start,end):
        return _k(end) in self.component(start)

    def shortest_path(self,start,goals, *, elbow_cost=0.0, pipe_cost=1.0):
        """Поиск от А до любого Б с учётом поворотов на всём пути.

        Направление предыдущего ребра — часть состояния Дейкстры;
        иначе добавочный отвод на стыке двух Segment был бы пропущен.
        """
        source=_k(start)
        if source not in self.graph:return None
        targets={_k(x) for x in goals}
        if source in targets:return ContinuousPath((source,),0.0,0)
        if not targets:return None
        queue=[(0.0,0.0,0,source,0)]
        best={(source,0):0.0}
        parent={}
        end=None
        while queue:
            cost,length,bends,node,direction=heapq.heappop(queue)
            state=(node,direction)
            if cost>best.get(state,math.inf)+1e-8:continue
            if node in targets:end=state;break
            for next_pt,span in self.graph.get(node,{}).items():
                new_dir=1 if abs(node[1]-next_pt[1])<TOL else 2
                turn=int(direction!=0 and direction!=new_dir)
                new_cost=cost+pipe_cost*span+elbow_cost*turn
                next_state=(next_pt,new_dir)
                if new_cost<best.get(next_state,math.inf)-1e-8:
                    best[next_state]=new_cost
                    parent[next_state]=state
                    heapq.heappush(queue,(new_cost,length+span,bends+turn,next_pt,new_dir))
        if end is None:return None
        chain=[]
        state=end
        while True:
            chain.append(state[0])
            if state==(source,0):break
            state=parent[state]
        chain.reverse()
        return ContinuousPath(tuple(chain),length,bends)
