"""Late 0.30 m AXIS spacing audit for nonstructural transit pipes.

This module never modifies a polyline. The catalogue solver may replace a
chosen LinkCandidate with another already generated candidate of similar
geometry. If none exists, leave an explicit diagnostic (no invisible redraw).
"""
from __future__ import annotations
import re

TARGET_AXIS_GAP = 0.30
STRUCTURAL={'Хребет','Перспективный хребет','Перспективное продление хребта',
            'Центральная плита: магистральная труба'}


def _external(s,axial_ids):
    if s.purpose in STRUCTURAL or s.purpose=='Подключение пожарного узла':return False
    m=re.match(r'^Отвод от скв\.\s*(\d+)',s.purpose)
    if m and int(m.group(1)) in axial_ids:return False
    return s.length>=1.0


def spacing_issues(segments, axial_ids=(), axis_gap=TARGET_AXIS_GAP):
    """(medium A, medium B, axis distance, overlapping metres, axes)."""
    pipes=[s for s in segments if _external(s,set(axial_ids))]
    errors=[]
    for i,a in enumerate(pipes):
        for b in pipes[i+1:]:
            if a.medium==b.medium or a.horizontal!=b.horizontal:continue
            aa=sorted((a.a.u,a.b.u) if a.horizontal else (a.a.v,a.b.v))
            bb=sorted((b.a.u,b.b.u) if b.horizontal else (b.a.v,b.b.v))
            overlap=min(aa[1],bb[1])-max(aa[0],bb[0])
            if overlap<1.-1e-6:continue
            ax=a.a.v if a.horizontal else a.a.u
            bx=b.a.v if b.horizontal else b.a.u
            gap=abs(ax-bx)
            if gap<axis_gap-1e-5:
                errors.append((a.medium,b.medium,round(gap,5),round(overlap,4),
                               'U' if a.horizontal else 'V',round(ax,5),round(bx,5),a.purpose,b.purpose))
    return errors


def summary(issues, limit=3):
    if not issues:return 'Межосевое расстояние транзитных труб ≥ 0,30 м: нарушений не выявлено.'
    rows=[]
    for a,b,g,overlap,axis,x,y,_,_ in issues[:limit]:
        rows.append(f'{a}/{b}: межосевое расстояние {g:.3f} м, параллельный участок {overlap:.1f} м')
    return (f'Межосевое расстояние транзитных труб 0,30 м не обеспечено в {len(issues)} пар(ах). '
            'Автоматическое смещение невозможно без изменения выбранной связи: '+'; '.join(rows))
