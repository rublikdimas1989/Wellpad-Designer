"""Экспорт инженерного 2D-плана: север всегда сверху; азимут НДС поворачивает геометрию."""
from pathlib import Path
from html import escape
import math
from routing import MEDIA,MEDIA_COLOR,MEDIA_RU,Pt,Result,well_connection_normal,well_dimensions,well_outlet,well_medium_outlets
from plan_view import local_to_scene
from pipe_render import primitives
from central_plate import plate_draw_data, capped_ports
from routing import PIPE_OD
from crossing_markers import overlay_primitives, visual_cover_primitives


def export_svg(r:Result,filename,pipe_mode:bool=False,show_well_guards:bool=False,show_main_guards:bool=False):
    width,height=1800,1120
    left,top,right,bottom=90,205,1710,910
    pts=[w.point for w in r.wells]+[r.center,r.tie,r.fire]
    for s in r.segments:pts.extend((s.a,s.b))
    for q in (Pt(r.head_u,min(r.lanes.values())-1),Pt(r.head_u,max(r.lanes.values())+1)):
        pts.append(q)
    scene=[local_to_scene(r.frame,pt,r.project.coordinate_mode) for pt in pts]
    xmin,xmax=min(q[0] for q in scene),max(q[0] for q in scene)
    ymin,ymax=min(q[1] for q in scene),max(q[1] for q in scene)
    midx,midy=(xmin+xmax)/2,(ymin+ymax)/2
    k=min((right-left)/max(xmax-xmin+35,1),(bottom-top)/max(ymax-ymin+35,1))
    cx,cy=(left+right)/2,(top+bottom)/2
    def xy(q):
        e,n=local_to_scene(r.frame,q,r.project.coordinate_mode)
        return cx+(e-midx)*k,cy-(n-midy)*k
    def line(a,b,color='#556575',w=1,dash=None):
        x1,y1=xy(a);x2,y2=xy(b)
        return (f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                f'stroke="{color}" stroke-width="{w}" '+(f'stroke-dasharray="{dash}" ' if dash else '')+'/>' )
    def label(x,y,s,size=15,color='#172b3a',anchor='start',weight='normal'):
        return (f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" '
                f'font-family="Arial,sans-serif" font-size="{size}" font-weight="{weight}" '
                f'fill="{color}">{escape(str(s))}</text>')
    out=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
         '<rect width="100%" height="100%" fill="white"/>',
         '<rect x="25" y="25" width="1750" height="1065" fill="none" stroke="#223443" stroke-width="1.4"/>',
         label(75,76,'ПЛАН ТРУБОПРОВОДОВ КУСТОВОЙ ПЛОЩАДКИ · ХРЕБЕТ',28,weight='bold'),
         label(75,109,'ВИД СВЕРХУ · центральная плита 6×1,75 м · осевые плиты 3×1,75 м · транзитные опоры 1,2×3 м',18),
         label(75,140,'Внеосевые подключения — к ближайшей допустимой линии своей среды. К врезке — только продуктовая нефть.',15,'#526579'),
         f'<rect x="{left}" y="{top}" width="{right-left}" height="{bottom-top}" fill="#ffffff" stroke="#b9c5d1"/>']
    # World grid: E to the right, N up.
    out.append('<g id="coordinate-grid">')
    step=10 if k>5 else 20 if k>2.5 else 50
    for E in range(math.floor((midx-(right-left)/(2*k))/step)*step,
                   math.ceil((midx+(right-left)/(2*k))/step)*step+step,step):
        x=cx+(E-midx)*k
        out.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="#e2eaf1" stroke-width=".8"/>')
    for N in range(math.floor((midy-(bottom-top)/(2*k))/step)*step,
                   math.ceil((midy+(bottom-top)/(2*k))/step)*step+step,step):
        y=cy-(N-midy)*k
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#e2eaf1" stroke-width=".8"/>')
    out.append('</g>')
    all_u=[w.point.u for w in r.wells]
    out.append('<g id="nds-axis">')
    out.append(line(Pt(0,0),Pt(max([0]+all_u+[r.trunk_end_u])+16,0),'#ce4e52',1.6,'13 6 2 6'))
    x,y=xy(Pt(max(all_u+[r.trunk_end_u])+16,0))
    out.append(label(x+8,y-10,'ОСЬ НДС',14,'#b53c42'))
    out.append('</g>')
    if show_well_guards:
        from routing import WELL_GUARD_RADIUS
        out.append('<g id="well-protection-5m" fill="none">')
        for wp in r.wells:
            x,y=xy(wp.point)
            out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{WELL_GUARD_RADIUS*k:.2f}" '
                       'fill="none" stroke="#c78c37" stroke-width="1.4" stroke-dasharray="5 4"/>')
        out.append('</g>')
    # Optional exclusion strips around the actual (not hypothetical) main
    # infrastructure. Same local geometry as the solver.
    if show_main_guards and r.central_plate is not None:
        from structural_clearance import infrastructure_buffers, TRUNK
        from types import SimpleNamespace
        structural=SimpleNamespace(trunk_segments=[s for s in r.segments if s.purpose in TRUNK])
        out.append('<g id="structural-guards-1point5m">')
        for area in infrastructure_buffers(structural,r.central_plate):
            x0,x1,y0,y1=area.expanded()
            points=' '.join(f'{x:.2f},{y:.2f}' for x,y in map(xy,
                  (Pt(x0,y0),Pt(x1,y0),Pt(x1,y1),Pt(x0,y1))))
            out.append(f'<polygon points="{points}" fill="#d9e4ed" fill-opacity="0.12" '
                       f'stroke="#708aa4" stroke-dasharray="5 4" stroke-width="1.3"/>')
        out.append('</g>')
    plate=r.central_plate
    if plate is not None:
        points=' '.join(f'{x:.2f},{y:.2f}' for x,y in
                        [xy(Pt(u,v)) for u,v in plate_draw_data(plate)])
        out.append('<g id="central-plate-6x1.75">')
        out.append(f'<polygon points="{points}" fill="#d9e3ec" stroke="#4f6f8a" stroke-width="2"/>')
        out.append('</g>')
    junction_plates=[slab for slab in r.slabs if slab.slab_type=='axial_junction']
    branch_plates=[slab for slab in r.slabs if slab.slab_type=='axial_branch']
    trunk_mid_plates=[slab for slab in r.slabs if slab.slab_type=='trunk_intermediate']
    endpoint_plates=[slab for slab in r.slabs if slab.slab_type in ('terminal_node','offaxis_branch','fire_terminal_node')]
    transit_plates=[slab for slab in r.slabs if slab.slab_type in ('transit_run','fire_transit_run')]
    fitting_plates=[slab for slab in r.slabs if slab.slab_type=='fitting_support']
    support_plates=[slab for slab in r.slabs if slab.slab_type!='central']
    out.append('<g id="axial-junction-plates-3x1.75">')
    for slab in junction_plates:
        points=' '.join(f'{x:.2f},{y:.2f}' for x,y in
                        [xy(Pt(u,v)) for u,v in
                         ((slab.u_start,slab.v_min),(slab.u_end,slab.v_min),
                          (slab.u_end,slab.v_max),(slab.u_start,slab.v_max))])
        out.append(f'<polygon points="{points}" fill="#d9e3ec" stroke="#4f6f8a" stroke-width="2"/>')
    out.append('</g>')
    out.append('<g id="axial-branch-plates-1.2x6">')
    for slab in branch_plates:
        points=' '.join(f'{x:.2f},{y:.2f}' for x,y in
                        [xy(Pt(u,v)) for u,v in
                         ((slab.u_start,slab.v_min),(slab.u_end,slab.v_min),
                          (slab.u_end,slab.v_max),(slab.u_start,slab.v_max))])
        out.append(f'<polygon points="{points}" fill="#d9e3ec" stroke="#4f6f8a" stroke-width="2"/>')
    out.append('</g>')
    out.append('<g id="intermediate-trunk-plates">')
    for slab in trunk_mid_plates:
        points=' '.join(f'{x:.2f},{y:.2f}' for x,y in
                        [xy(Pt(u,v)) for u,v in
                         ((slab.u_start,slab.v_min),(slab.u_end,slab.v_min),
                          (slab.u_end,slab.v_max),(slab.u_start,slab.v_max))])
        out.append(f'<polygon points="{points}" fill="#d9e3ec" stroke="#4f6f8a" stroke-width="2"/>')
    out.append('</g>')
    boundary_plates=[slab for slab in r.slabs
                     if slab.slab_type in ('boundary_fixed','fire_boundary_fixed')]
    out.append('<g id="boundary-fixed-plates-1point5m">')
    for slab in boundary_plates:
        points=' '.join(f'{x:.2f},{y:.2f}' for x,y in
                        [xy(Pt(u,v)) for u,v in
                         ((slab.u_start,slab.v_min),(slab.u_end,slab.v_min),
                          (slab.u_end,slab.v_max),(slab.u_start,slab.v_max))])
        out.append(f'<polygon points="{points}" fill="#efe1c7" stroke="#a67b3e" stroke-width="2"/>')
    out.append('</g>')
    if pipe_mode:
        out.append('<g id="pipe-contours-and-fittings">')
        for primitive in primitives(r.segments,lambda q:xy(Pt(*q)),k):
            if primitive.kind=='line':
                (x1,y1),(x2,y2)=primitive.points
                dash=(f' stroke-dasharray="{primitive.dash}"' if primitive.dash else '')
                out.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                           f'stroke="{primitive.stroke}" stroke-width="{primitive.width:.2f}"{dash}/>')
            elif primitive.kind=='polygon':
                coords=' '.join(f'{x:.2f},{y:.2f}' for x,y in primitive.points)
                dash=(f' stroke-dasharray="{primitive.dash}"' if primitive.dash else '')
                out.append(f'<polygon points="{coords}" stroke="{primitive.stroke}" '
                           f'stroke-width="{primitive.width:.2f}" fill="{primitive.fill or "none"}"{dash}/>')
            elif primitive.kind=='ellipse':
                (x,y),(rx,ry)=primitive.points
                dash=(f' stroke-dasharray="{primitive.dash}"' if primitive.dash else '')
                out.append(f'<ellipse cx="{x:.2f}" cy="{y:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" '
                           f'stroke="{primitive.stroke}" stroke-width="{primitive.width:.2f}" fill="{primitive.fill or "none"}"{dash}/>')
        out.append('</g>')
    else:
        for m in MEDIA:
            out.append(f'<g id="pipe-{m}">')
            for s in r.segments:
                if s.medium==m:
                    out.append(line(s.a,s.b,MEDIA_COLOR[m],3.2 if s.phase=='active' else 2.6,'10 6' if s.phase=='future' else None))
            out.append('</g>')
    if r.project.auto_overpasses and r.crossing_markup is not None:
        out.append('<g id="crossing-visual-cover">')
        for primitive in visual_cover_primitives(r.crossing_markup,lambda q:xy(q),k,detailed=pipe_mode,central_plate=plate,support_slabs=support_plates):
            if primitive.kind=='line':
                (x1,y1),(x2,y2)=primitive.points
                dash=(f' stroke-dasharray="{primitive.dash}"' if primitive.dash else '')
                out.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                           f'stroke="{primitive.stroke}" stroke-width="{primitive.width:.2f}" stroke-linecap="round"{dash}/>')
            elif primitive.kind=='polygon':
                coords=' '.join(f'{x:.2f},{y:.2f}' for x,y in primitive.points)
                out.append(f'<polygon points="{coords}" stroke="{primitive.stroke}" '
                           f'stroke-width="{primitive.width:.2f}" fill="{primitive.fill or "none"}"/>')
        out.append('</g>')
        out.append('<g id="crossing-markers">')
        for item in overlay_primitives(r.crossing_markup,lambda q:xy(q),k):
            if item[0]=='line':
                _,(x1,y1),(x2,y2),color,width=item
                out.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" stroke="{color}" stroke-width="{width}"/>')
            elif item[0]=='text':
                _,(x,y),label_text,color,size=item
                out.append(label(x,y,label_text,size,color,weight='bold'))
        out.append('</g>')
    if plate is not None:
        out.append('<g id="central-plate-plugs">')
        used_plate_media=tuple(sorted({s.medium for s in r.segments if s.purpose=='Центральная плита: магистральная труба'}))
        for med,pos,face in capped_ports(plate,r.segments,r.lanes,used_plate_media):
            # Заглушка отдельно от прямой трубы, по геометрии её торца.
            w=max(PIPE_OD[med]/2,.07)
            corners=(Pt(pos.u-.12,pos.v-w),Pt(pos.u+.12,pos.v-w),
                     Pt(pos.u+.12,pos.v+w),Pt(pos.u-.12,pos.v+w))
            poly=' '.join(f'{x:.2f},{y:.2f}' for x,y in map(xy,corners))
            out.append(f'<polygon points="{poly}" fill="{MEDIA_COLOR[med]}" '
                       f'stroke="#263647" stroke-width="1.2"/>')
        out.append('</g>')
    out.append('<g id="well-symbols">')
    for wp in r.wells:
        x,y=xy(wp.point)
        clr={'active':'#d13949','future':'#c79821','inactive':'#97a0a9'}[wp.well.status]
        # Габариты символа совпадают с геометрией выходной точки в метрах.
        hu,hv=well_dimensions(wp)
        corners=[xy(Pt(wp.point.u+du,wp.point.v+dv)) for du,dv in
                 ((-hu,-hv),(hu,-hv),(hu,hv),(-hu,hv))]
        coords=' '.join(f'{px:.1f},{py:.1f}' for px,py in corners)
        dash='stroke-dasharray="6 3"' if wp.well.status=='future' else ''
        out.append(f'<polygon points="{coords}" fill="white" stroke="{clr}" stroke-width="2" {dash}/>')
        for a,b in ((0,2),(1,3)):
            x1,y1=corners[a];x2,y2=corners[b]
            out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{clr}" stroke-width="1.1"/>')
        for med,outlet in well_medium_outlets(wp,r.trunk_side,r.center,r.project.lane_gap).items():
            px,py=xy(outlet)
            out.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="3" fill="{MEDIA_COLOR[med]}" stroke="white" stroke-width="1"/>')
        out.append(label(x,y+40,f'№{wp.well.number}',13,'#a633aa','middle','bold'))
        if wp.well.status=='future':out.append(label(x,y+49,'персп.',10,'#9b7220','middle'))
    out.append('</g>')
    out.append('<g id="nodes">')
    if plate is not None:
        x,y=xy(Pt(plate.center_u,plate.center_v))
        out.append(label(x,y-13,'ЦЕНТРАЛЬНАЯ ПЛИТА 6×1,75 м',13,'#1e4f6e','middle','bold'))
    for q,name,color in ((r.tie,'ТОЧКА ВРЕЗКИ (ТОЛЬКО ПРОДУКТОВАЯ НЕФТЬ)','#1d2e39'),
                         (r.fire,'ПОЖАРНЫЙ УЗЕЛ','#b03d43')):
        x,y=xy(q)
        out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="white" stroke="{color}" stroke-width="2"/>')
        out.append(label(x+12,y-12,name,12,color,weight='bold'))
    out.append('</g>')
    out.append('<g id="legend">')
    for i,m in enumerate(MEDIA):
        x=85+(i%3)*545;y=962+(i//3)*35
        out.append(f'<line x1="{x}" y1="{y}" x2="{x+36}" y2="{y}" stroke="{MEDIA_COLOR[m]}" stroke-width="4"/>')
        out.append(label(x+49,y+5,f'{i+1}. {MEDIA_RU[m]}',15))
    out.append('</g>')
    out.append('<g id="north-arrow">')
    out.append(f'<line x1="1660" y1="237" x2="1660" y2="185" stroke="#152938" stroke-width="3"/>')
    out.append('<path d="M1660,175 L1653,192 L1667,192 Z" fill="#152938"/>')
    out.append(label(1660,162,'С',18,'#152938','middle','bold'))
    out.append(label(1650,264,f'НДС {r.project.azimuth:g}°',13,'#152938','end'))
    out.append('</g>')
    out.append(label(85,1056,f'Труб: {r.metrics.get("pipe_m",0):.1f} м · плит осевых подключений {len(junction_plates)} · плит отпаек {len(branch_plates)} · промежуточных хребтовых {len(trunk_mid_plates)} · локальных 1,2×3 м {len(endpoint_plates)} · транзитных 1,2×3 м {len(transit_plates)} · плит под узлами {len(fitting_plates)} · соединительных хребтовых {int(r.metrics.get("trunk_tap_plate_count",0))} · общих коридоров ~{r.metrics.get("shared_corridor_m",0):.1f} м'
                     f' · групп {len(r.routing_groups)} · J={r.metrics.get("optimized_score",0):.1f} усл. м · коллизий {len(r.crossings)}. Эскиз, не РД.',14,'#5e6c78'))
    out.append('</svg>')
    Path(filename).write_text('\n'.join(out),encoding='utf-8')
