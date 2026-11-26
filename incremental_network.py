"""v87: explicit finite-grid shortest-path exploration and two-phase network design.

Only the independent stage is evaluated before the Optimize button. Physical
tee-joining and multi-medium common-support alternatives are introduced during
Optimize. The legacy permitted-port catalogue is used internally as a *port
index*, not as a UI stage or as the authority on the final path shape.

Finite grid and bounded optimisation do NOT establish global optimality.
"""
from __future__ import annotations

from dataclasses import replace
from collections import defaultdict
from heapq import heappush, heappop
from math import floor, ceil, inf
from time import monotonic

from routing import (Pt, Project, PIPE_OD, protected_line_ok,
                     trunk_inner_zone, inner_trunk_parallel, no_immediate_uturn)
from planning_workflow import make_survey,generate_links, LinkCandidate
from path_preference import compact_path,path_length,path_bends
from catalog_solver import (_make_result, link_signature, _available,
                            build_from_catalog, audit_catalog_result)
from network_cost import estimate_score
from structural_clearance import clearance_violations, infrastructure_buffers, _intersection_length

STEP=1.0              # small orthogonal graph edges; special coordinates are retained
GRID_SEARCH_LIMIT=4400
# «Рассчитать»: до 100 самых дешёвых НЕЗАВИСИмых путей на (элемент, среда).
PATHS_PER_SLOT=100
ROUTES_PER_ELEMENT=PATHS_PER_SLOT  # legacy alias
RUN_LIMIT_S=45.0        # bounded UI response; the status discloses incomplete exploration
MAX_OPT_TRIALS=120


def _finite_grid(a,b,wells, margin=12.0):
    """Rectilinear grid: 1 m nodes plus exact ports and obstacle tangencies."""
    lo_u=min(a.u,b.u)-margin;hi_u=max(a.u,b.u)+margin
    lo_v=min(a.v,b.v)-margin;hi_v=max(a.v,b.v)+margin
    def axis(lo,hi,own0,own1,center):
        integer={float(x) for x in range(floor(lo),ceil(hi)+1)}
        integer.update((own0,own1))
        for wp in wells:
            k=(wp.point.u if center=='u' else wp.point.v)
            for offset in (-10.55,-10.15,10.15,10.55):
                value=round(k+offset,5)
                if lo<=value<=hi:integer.add(value)
        return sorted(integer)
    return axis(lo_u,hi_u,a.u,b.u,'u'),axis(lo_v,hi_v,a.v,b.v,'v')


def _walk(start,finish,survey,*,owner=None,plate=None,limit=GRID_SEARCH_LIMIT,
          report=None):
    """A* on short orthogonal edges; objective is pipe RUB + bend RUB.

    Connect exact start and terminal gate to the grid. All edges are checked
    against wells, forbidden inner-trunk parallel transit and 1.5 m buffers.
    """
    if abs(start.u-finish.u)+abs(start.v-finish.v)<1e-7:
        return (start,),0,False
    xs,ys=_finite_grid(start,finish,survey.wells)
    if len(xs)*len(ys)>125000:
        return (),0,True
    xi={round(v,5):k for k,v in enumerate(xs)}
    yi={round(v,5):k for k,v in enumerate(ys)}
    source=(xi[round(start.u,5)],yi[round(start.v,5)])
    goal=(xi[round(finish.u,5)],yi[round(finish.v,5)])
    zone=trunk_inner_zone(survey.lanes,survey.trunk_side,
       min((min(s.a.u,s.b.u) for s in survey.trunk_segments),default=0.),survey.wells)
    forbidden=tuple(area.expanded() for area in infrastructure_buffers(survey,plate)) if plate else ()
    edge_cache={}
    def allowed(i,j,k,l):
        ident=(min(i,k),min(j,l),max(i,k),max(j,l))
        if ident in edge_cache:return edge_cache[ident]
        a=Pt(xs[i],ys[j]);b=Pt(xs[k],ys[l])
        ok=(protected_line_ok(a,b,survey.wells,owner=owner)
            and not inner_trunk_parallel(a,b,zone)
            and not any(_intersection_length(a,b,rect)>1e-6 for rect in forbidden))
        edge_cache[ident]=ok
        return ok
    # Direction 0 initial; 1 horizontal; 2 vertical
    origin=(source[0],source[1],0)
    best={origin:0.}
    parents={}
    pending=[]
    heuristic=lambda i,j: survey.project.pipe_cost_per_m*(abs(xs[i]-finish.u)+abs(ys[j]-finish.v))
    heappush(pending,(heuristic(*source),0.,origin))
    checked=0;last=None
    while pending and checked<limit:
        _,g,state=heappop(pending)
        if g>best.get(state,inf)+1e-7:continue
        i,j,direction=state
        checked+=1
        if report and checked%500==0:
            report('explore', explored=checked, point=(xs[i],ys[j]),end=(finish.u,finish.v))
        if (i,j)==goal:
            last=state;break
        for k,l,nd in ((i-1,j,1),(i+1,j,1),(i,j-1,2),(i,j+1,2)):
            if k<0 or k>=len(xs) or l<0 or l>=len(ys):continue
            if not allowed(i,j,k,l):continue
            distance=abs(xs[k]-xs[i])+abs(ys[l]-ys[j])
            ng=g+distance*survey.project.pipe_cost_per_m+(survey.project.elbow_cost if direction and direction!=nd else 0.)
            nxt=(k,l,nd)
            if ng>=best.get(nxt,inf)-1e-7:continue
            best[nxt]=ng;parents[nxt]=state
            heappush(pending,(ng+heuristic(k,l),ng,nxt))
    if last is None:return (),checked,checked>=limit
    path=[];current=last
    while True:
        i,j,_=current;path.append(Pt(xs[i],ys[j]))
        if current==origin:break
        current=parents[current]
    return compact_path(tuple(reversed(path))),checked,False


def _same_route(a,b):
    return tuple((round(p.u,4),round(p.v,4)) for p in a)==tuple((round(p.u,4),round(p.v,4)) for p in b)


def _route_leg(survey,q,*,plate,notify=None):
    """Retry an independently permitted source and target with small-step A*."""
    if q.source=='fire' or any(w.well.number==int(q.source.split(':')[1]) and w.well.on_axis
          for w in survey.wells) if q.source.startswith('well:') else q.source=='fire':
        return None,0,False
    if len(q.path)<2:return None,0,False
    # First four metres from a well are straight and reserved for an outlet plate.
    if q.source.startswith('well:') and q.start_side in ('u+','u-','v+','v-'):
        du,dv={'u+':(1,0),'u-':(-1,0),'v+':(0,1),'v-':(0,-1)}[q.start_side]
        lead=Pt(q.start.u+du*4.,q.start.v+dv*4.)
        owner=int(q.source.split(':')[1])
    else:lead=q.start;owner=None
    # Route-search goal is outside the reserved 1.5 m buffer. The segment
    # from this gate to the real port is an allowed FINAL attachment only.
    from structural_clearance import infrastructure_buffers,CLEARANCE
    areas={area.kind:area for area in infrastructure_buffers(survey,plate)}
    if q.target.startswith('virtual:plate:'):
        area=areas['central']
        if q.end.v>=area.y1-1e-5:
            gate=Pt(q.end.u,area.y1+CLEARANCE+.08)
        elif q.end.v<=area.y0+1e-5:
            gate=Pt(q.end.u,area.y0-CLEARANCE-.08)
        else:
            # Fire/end port: covered by the separate fire implementation.
            gate=q.path[-2] if len(q.path)>2 else q.end
    elif q.target.startswith('base:trunk:'):
        area=areas['trunk']
        if ':end:' in q.target or q.end.u<=area.x0+1e-5 or q.end.u>=area.x1-1e-5:
            # Attach from the far (non-central) end of the spine.
            right=(':right:' in q.target)
            gate=Pt(area.x0-CLEARANCE-.08 if right else area.x1+CLEARANCE+.08,q.end.v)
        else:
            sign=survey.trunk_side
            gate=Pt(q.end.u,(area.y1+CLEARANCE+.08) if sign>0 else (area.y0-CLEARANCE-.08))
    else:
        gate=q.path[-2] if len(q.path)>=3 else q.end
    if not protected_line_ok(gate,q.end,survey.wells,owner=owner):
        return None,0,False
    middle,expanded,limited=_walk(lead,gate,survey,owner=owner,plate=plate,report=notify)
    if not middle:return None,expanded,limited
    full=compact_path((q.start,*middle,q.end))
    if not no_immediate_uturn(full):return None,expanded,limited
    if any(not protected_line_ok(a,b,survey.wells,owner=owner) for a,b in zip(full,full[1:])):
        return None,expanded,limited
    trial=replace(q,path=full,length=path_length(full),elbows=path_bends(full),
                  reason='Дискретный A* поиск: шаг 1 м + точные геометрические координаты портов')
    if clearance_violations(trial,survey,plate):return None,expanded,limited
    if _same_route(q.path,trial.path):return None,expanded,limited
    return trial,expanded,limited


def _independent_cost(q,p):
    return p.pipe_cost_per_m*q.length+p.elbow_cost*q.elbows


def _shortlist(links,p,limit=PATHS_PER_SLOT):
    """Up to ``limit`` cheapest independent geometries, with light diversity.

    No bundling/tee logic. Used both as A* seeds and as the retained pool.
    """
    ranked=sorted((q for q in links if q.path and q.status!='недопустим'),
         key=lambda q:(_independent_cost(q,p),q.elbows,q.length,q.target,q.start_side))
    chosen={}
    # Keep cheapest first.
    for q in ranked:
        chosen[link_signature(q)]=q
        if len(chosen)>=limit:break
    # Sprinkle a few spatially different ports so the pool is not one cluster.
    buckets=defaultdict(list)
    for q in ranked:
        family='plate' if q.target.startswith('virtual:plate') else 'trunk'
        side='left' if ':left:' in q.target else 'right' if ':right:' in q.target else 'both'
        key=(family,side,q.start_side,q.medium)
        if len(buckets[key])<3:
            buckets[key].append(q)
            chosen[link_signature(q)]=q
    if ranked:
        for frac in (0.1,0.25,0.5,0.75,0.9):
            q=ranked[min(len(ranked)-1,int((len(ranked)-1)*frac))]
            chosen[link_signature(q)]=q
    ordered=sorted(chosen.values(),
                   key=lambda q:(_independent_cost(q,p),q.elbows,q.length,q.target))
    return ordered[:limit]


def _trim_pool(catalog,project,limit=PATHS_PER_SLOT):
    """Keep at most ``limit`` cheapest independent paths per (source, medium)."""
    from collections import defaultdict
    by_slot=defaultdict(list)
    for key,links in catalog.items():
        for q in links:
            if not q.path or q.status=='недопустим':continue
            by_slot[(key,q.medium)].append(q)
    trimmed={key:[] for key in catalog}
    for (key,medium),rows in by_slot.items():
        ranked=sorted(rows,key=lambda q:(_independent_cost(q,project),q.elbows,q.length,q.target))
        seen=set();kept=[]
        for q in ranked:
            sig=link_signature(q)
            if sig in seen:continue
            seen.add(sig);kept.append(q)
            if len(kept)>=limit:break
        trimmed.setdefault(key,[]).extend(kept)
    return trimmed


def find_independent_routes(project, survey=None, *, progress=None,
                            allow_extra_paths=True):
    """Build per-element independent path pools (no joins, no bundles).

    For every active element the routine gathers port routes and optional A*
    variants, then keeps the cheapest PATHS_PER_SLOT geometries per medium.
    """
    survey=survey or make_survey(project)
    result={};stats={'targets_checked':0,'paths_examined':0,'grid_nodes_expanded':0,
                     'grid_limit_hits':0,'grid_step_m':STEP,'finite_search':True,
                     'paths_per_slot':PATHS_PER_SLOT}
    for el in survey.elements:
        if el.status=='inactive':continue
        routes=generate_links(survey,el.key)
        stats['targets_checked']+=len(routes)
        viable=[q for q in routes if q.path and q.status!='недопустим']
        if not viable:result[el.key]=routes;continue
        extras=[]
        # Explore more A* variants so the retained top-100 is not only L-paths.
        seeds=_shortlist(viable,project,limit=min(40,max(12,PATHS_PER_SLOT//3)))
        if allow_extra_paths and (el.kind=='tie' or (el.kind=='well' and
                        not next(w for w in survey.wells if w.well.number==el.well_number).well.on_axis)):
            for n,q in enumerate(seeds):
                name='right' if ':right:' in q.target else 'left'
                plate=next(pl for tag,pl in survey.plate_variants if tag==name)
                def notify(*args,**kw):
                    if progress:progress('search_step',source=el.key,medium=q.medium,
                        candidate=n+1,total=len(seeds),**kw)
                candidate,expanded,limited=_route_leg(survey,q,plate=plate,notify=notify)
                stats['grid_nodes_expanded']+=expanded
                stats['grid_limit_hits']+=int(limited)
                stats['paths_examined']+=1
                if candidate:extras.append(candidate)
                if progress and n%3==0:
                    progress('route_candidate',source=el.key,medium=q.medium,
                             candidate=n+1,total=len(seeds),path=q.path,
                             cost=round(_independent_cost(q,project),0))
        merged=list({link_signature(q):q for q in (*routes,*extras)}.values())
        # Hard cap: 100 cheapest independent paths per medium of this element.
        by_med=defaultdict(list)
        for q in merged:
            if q.path and q.status!='недопустим':
                by_med[q.medium].append(q)
        kept=[]
        for medium,rows in by_med.items():
            kept.extend(_shortlist(rows,project,limit=PATHS_PER_SLOT))
        # Preserve rejected/placeholder entries for diagnostics.
        rejected=[q for q in routes if not q.path or q.status=='недопустим']
        result[el.key]=list({link_signature(q):q for q in (*kept,*rejected)}.values())
        stats.setdefault('pool_sizes',{})[el.key]=sum(
            1 for q in result[el.key] if q.path and q.status!='недопустим')
        if progress:progress('source_done',source=el.key,medium=', '.join(el.media),
                             candidate=stats['pool_sizes'][el.key],total=PATHS_PER_SLOT,
                             path=min(viable,key=lambda q:_independent_cost(q,project)).path)
    result=_trim_pool(result,project,limit=PATHS_PER_SLOT)
    return survey,result,stats


def _selected_links(result,catalog):
    bysig={link_signature(q):q for variants in catalog.values() for q in variants}
    return [bysig[sig] for sig in result.decision_trace.get('_catalog_hashes',()) if sig in bysig]


def calculate_independent(project,survey=None,*,progress=None,app_root=None,name_hint=''):
    """Совместимость: делегирует в calc_optimize.run_calculate."""
    from calc_optimize import run_calculate
    return run_calculate(project,survey,progress=progress,app_root=app_root,name_hint=name_hint)


def optimize_network(project,survey,catalog,initial,*,progress=None,limit=MAX_OPT_TRIALS):
    """Совместимость: делегирует в calc_optimize.run_optimize."""
    from calc_optimize import run_optimize
    return run_optimize(project,survey,catalog,initial,progress=progress,limit=limit)