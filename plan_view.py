"""Преобразование для генплана СЕВЕР ВВЕРХ, восток вправо.

Направление оси НДС задаётся дирекционным углом от севера по часовой
стрелке. В отличие от локальных координат U/V карта НЕ вращается
вместе с хребтом: меняется реальное направление линии на чертеже.

Сцена (E,N) = метры относительно первой скважины.
"""
from __future__ import annotations
from dataclasses import dataclass
from routing import Frame, Pt


def local_to_scene(frame:Frame, pt:Pt, mode:str) -> tuple[float,float]:
    x,y=frame.xy(pt)
    if mode=='x_north':
        return y-frame.y0, x-frame.x0
    return x-frame.x0,y-frame.y0


def scene_to_world(frame:Frame, e:float, n:float, mode:str) -> tuple[float,float]:
    if mode=='x_north':
        return frame.x0+n,frame.y0+e
    return frame.x0+e,frame.y0+n


def scene_to_local(frame:Frame, e:float, n:float, mode:str)->Pt:
    return frame.uv(*scene_to_world(frame,e,n,mode))


@dataclass
class MapView:
    frame:Frame
    mode:str
    cx:float=0.0  # Сцена E, м
    cy:float=0.0  # Сцена N, м
    zoom:float=1.0
    width:int=900
    height:int=600

    def to_screen(self,p:Pt)->tuple[float,float]:
        east,north=local_to_scene(self.frame,p,self.mode)
        return self.width/2+(east-self.cx)*self.zoom, self.height/2-(north-self.cy)*self.zoom

    def from_screen(self,x:float,y:float)->Pt:
        e=self.cx+(x-self.width/2)/self.zoom
        n=self.cy-(y-self.height/2)/self.zoom
        return scene_to_local(self.frame,e,n,self.mode)

    def world_from_screen(self,x:float,y:float)->tuple[float,float]:
        return self.frame.xy(self.from_screen(x,y))

    def fit(self,pts:list[Pt],pad_px:float=95,pad_m:float=20):
        scene=[local_to_scene(self.frame,p,self.mode) for p in pts]
        xs=[p[0] for p in scene];ys=[p[1] for p in scene]
        xmin,xmax=min(xs),max(xs);ymin,ymax=min(ys),max(ys)
        self.cx=(xmin+xmax)/2
        self.cy=(ymin+ymax)/2
        self.zoom=max(.02,min((self.width-2*pad_px)/max(xmax-xmin+pad_m,1),
                               (self.height-2*pad_px)/max(ymax-ymin+pad_m,1)))

    def zoom_at(self,x,y,factor):
        east=self.cx+(x-self.width/2)/self.zoom
        north=self.cy-(y-self.height/2)/self.zoom
        self.zoom=max(.015,min(self.zoom*factor,150))
        self.cx=east-(x-self.width/2)/self.zoom
        self.cy=north+(y-self.height/2)/self.zoom
