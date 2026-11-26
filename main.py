"""Графический интерфейс для эскизной трассировки. Запуск: python main.py"""
from __future__ import annotations

import math
import queue
import threading
from copy import deepcopy
from dataclasses import asdict
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from central_plate import plate_draw_data, capped_ports
from export_svg import export_svg
from pipe_render import primitives
from crossing_markers import overlay_primitives, export_crossings_csv, visual_cover_primitives
from plan_view import MapView, local_to_scene
from project_store import ProjectStore
from centering import center_window
from startup_ui import ProjectHome
from ui_theme import PALETTE, apply_theme, style_spinbox
from materials_catalog import export_materials_csv, takeoff
from route_inspector import inspect_segment
from candidate_space import candidate_points, format_points
from planning_workflow import make_survey, preview_result, generate_links, link_stats
from routing import (
    KIND_RU, MEDIA, MEDIA_COLOR, MEDIA_RU, PIPE_OD, STATUS_RU, WELL_KINDS,
    Project, Pt, Result, Well, axial_u, export_csv, well_connection_normal, well_dimensions, well_outlet, well_medium_outlets, WELL_GUARD_RADIUS, well_connections,
)

KIND_FROM_RU = {v:k for k,v in KIND_RU.items()}
STATUS_FROM_RU = {v:k for k,v in STATUS_RU.items()}
BACKGROUND = PALETTE["drawing"]


def num(text: str, title: str, optional: bool = False) -> float | None:
    v = text.strip().replace(" ", "").replace(",", ".")
    if optional and not v:
        return None
    if not v:
        raise ValueError(f"Заполните поле «{title}»")
    try:
        value = float(v)
    except ValueError as e:
        raise ValueError(f"Поле «{title}»: требуется число") from e
    if not math.isfinite(value):
        raise ValueError(f"Поле «{title}»: неверное число")
    return value


def fmt(value: float | None) -> str:
    if value is None:
        return ""
    return format(value, ".15g")


class WellDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, well: Well, callback):
        super().__init__(master)
        self.configure(background=PALETTE["window"])
        self.well = well
        self.callback = callback
        self.title(f"Скважина №{well.number}")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        content = ttk.Frame(self, padding=18)
        content.grid(row=0, column=0)
        self.type_var = tk.StringVar(value=KIND_RU[well.kind])
        self.status_var = tk.StringVar(value=STATUS_RU[well.status])
        self.axis_var = tk.BooleanVar(value=well.on_axis)
        self.gas_var = tk.BooleanVar(value=well.gas)
        self.orient_var=tk.StringVar(value='Длинная вдоль НДС' if well.orientation=='parallel' else 'Длинная поперёк НДС')
        self.lock_var=tk.BooleanVar(value=well.rotation_locked)
        self.exit_var=tk.StringVar(value={'auto':'Авто (все четыре стороны)',
             'u+':'Вдоль +U','u-':'Вдоль −U','v+':'Поперёк +V','v-':'Поперёк −V'}[well.exit_direction])
        self.planned_vars={m:tk.BooleanVar(value=(m in getattr(well,'planned_media',[]))) for m in ('fresh','pressure','gas','oil_product','oil_measure')}
        self.x_var = tk.StringVar(value=fmt(well.x))
        self.y_var = tk.StringVar(value=fmt(well.y))
        ttk.Label(content, text="Тип скважины").grid(row=0,column=0,sticky="w",pady=5)
        ttk.Combobox(content, textvariable=self.type_var, state="readonly", values=list(KIND_RU.values()),width=27).grid(row=0,column=1,pady=5)
        ttk.Label(content, text="Статус").grid(row=1,column=0,sticky="w",pady=5)
        ttk.Combobox(content, textvariable=self.status_var, state="readonly", values=list(STATUS_RU.values()),width=27).grid(row=1,column=1,pady=5)
        cb=ttk.Checkbutton(content,text="На оси НДС",variable=self.axis_var,command=self.toggle)
        cb.grid(row=2,column=0,columnspan=2,sticky="w",pady=8)
        if well.number == 1:
            cb.state(["disabled"])
        ttk.Label(content,text="X вне оси, м").grid(row=3,column=0,sticky="w",pady=5)
        self.x_entry=ttk.Entry(content,textvariable=self.x_var,width=29)
        self.x_entry.grid(row=3,column=1,pady=5)
        ttk.Label(content,text="Y вне оси, м").grid(row=4,column=0,sticky="w",pady=5)
        self.y_entry=ttk.Entry(content,textvariable=self.y_var,width=29)
        self.y_entry.grid(row=4,column=1,pady=5)
        if well.number != 1:
            ttk.Button(content,text="⌖ Указать координаты щелчком на плане",
                       command=self.pick_on_plan).grid(row=5,column=0,columnspan=2,sticky="ew",pady=5)
        ttk.Checkbutton(content,text="Нужна газовая линия от скважины",variable=self.gas_var).grid(row=6,column=0,columnspan=2,sticky="w",pady=10)
        ttk.Label(content,text='Перспективные линии (показываются пунктиром)').grid(row=7,column=0,columnspan=2,sticky='w',pady=(2,1))
        planned_frame=ttk.Frame(content)
        planned_frame.grid(row=8,column=0,columnspan=2,sticky='w')
        ttk.Checkbutton(planned_frame,text=MEDIA_RU['fresh'],variable=self.planned_vars['fresh']).grid(row=0,column=0,sticky='w',padx=(0,14))
        ttk.Checkbutton(planned_frame,text=MEDIA_RU['pressure'],variable=self.planned_vars['pressure']).grid(row=0,column=1,sticky='w')
        ttk.Checkbutton(planned_frame,text=MEDIA_RU['gas'],variable=self.planned_vars['gas']).grid(row=1,column=0,sticky='w',padx=(0,14))
        ttk.Checkbutton(planned_frame,text=MEDIA_RU['oil_product'],variable=self.planned_vars['oil_product']).grid(row=1,column=1,sticky='w')
        ttk.Checkbutton(planned_frame,text=MEDIA_RU['oil_measure'],variable=self.planned_vars['oil_measure']).grid(row=2,column=0,columnspan=2,sticky='w')
        ttk.Label(content,text="Ориентация ДЛИННОЙ стороны прямоугольника относительно НДС").grid(row=9,column=0,columnspan=2,sticky='w',pady=(8,1))
        ttk.Combobox(content,textvariable=self.orient_var,state='readonly',values=('Длинная вдоль НДС','Длинная поперёк НДС'),width=30).grid(row=10,column=0,columnspan=2,sticky='ew')
        ttk.Checkbutton(content,text='Зафиксировать направление (запретить автоповорот)',variable=self.lock_var).grid(row=11,column=0,columnspan=2,sticky='w',pady=4)
        ttk.Label(content,text='Сторона выхода труб (вне оси НДС)').grid(row=12,column=0,columnspan=2,sticky='w',pady=(5,1))
        ttk.Combobox(content,textvariable=self.exit_var,state='readonly',
                     values=('Авто (все четыре стороны)','Вдоль +U','Вдоль −U','Поперёк +V','Поперёк −V'),width=30).grid(row=13,column=0,columnspan=2,sticky='ew')
        ttk.Label(content,text="При статусе «Не обустраиваемая» линии не строятся. Для выбранного типа лишние перспективные галочки будут проигнорированы.",foreground=PALETTE["muted"],wraplength=440,justify='left').grid(row=14,column=0,columnspan=2,sticky="w",pady=(2,12))
        bottom=ttk.Frame(content)
        bottom.grid(row=15,column=0,columnspan=2,sticky="e")
        ttk.Button(bottom,text="Отмена",command=self.destroy).pack(side="right",padx=4)
        ttk.Button(bottom,text="Сохранить",command=self.submit).pack(side="right",padx=4)
        self.toggle()
        self.bind("<Escape>",lambda e:self.destroy())
        self.after_idle(lambda:center_window(self))
        self.focus_set()

    def toggle(self):
        state = "disabled" if self.axis_var.get() else "normal"
        self.x_entry.config(state=state)
        self.y_entry.config(state=state)

    def pick_on_plan(self):
        master=self.master
        # Снять модальный захват окна, иначе клик по генплану не дойдёт.
        self.grab_release()
        self.withdraw()
        def picked(x,y):
            self.axis_var.set(False)
            self.x_var.set(f"{x:.3f}")
            self.y_var.set(f"{y:.3f}")
            self.toggle()
            self.deiconify();self.lift();self.grab_set()
        def cancelled():
            self.deiconify();self.lift();self.grab_set()
        master.begin_pick(f"скважина №{self.well.number}",picked,cancelled)

    def submit(self):
        try:
            axis = self.axis_var.get()
            x = None if axis else num(self.x_var.get(),"X скважины")
            y = None if axis else num(self.y_var.get(),"Y скважины")
            kind=KIND_FROM_RU[self.type_var.get()]
            orient='parallel' if self.orient_var.get()=='Длинная вдоль НДС' else 'perpendicular'
            chosen_exit={'Авто (все четыре стороны)':'auto','Вдоль +U':'u+',
                         'Вдоль −U':'u-','Поперёк +V':'v+','Поперёк −V':'v-'}[self.exit_var.get()]
            if not axis and chosen_exit!='auto':
                orient='perpendicular' if chosen_exit[0]=='u' else 'parallel'
            preview_w=Well(self.well.number, kind,
                         STATUS_FROM_RU[self.status_var.get()], axis, x, y, self.gas_var.get(),
                         orient, self.lock_var.get())
            possible={m for m,_ in well_connections(preview_w)}
            if kind=='producer':possible.add('oil_measure')  # самостоятельная магистраль без прямых отводов
            planned=[m for m,var in self.planned_vars.items() if var.get() and m in possible]
            new_w = Well(self.well.number, kind,
                         STATUS_FROM_RU[self.status_var.get()], axis, x, y, self.gas_var.get(),
                         orient, self.lock_var.get(), planned, chosen_exit if not axis else 'auto')
            new_w.validate()
            self.callback(new_w)
            self.destroy()
        except Exception as e:
            messagebox.showerror("Ошибка в скважине",str(e),parent=self)


class Editor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Проектировщик кустовой площадки — ХРЕБЕТ v93c")
        self.withdraw()
        self.geometry("1510x910")
        self.minsize(1120,700)
        # Главный рабочий экран теперь разворачивается в open_managed_project.
        # Отложенное центрирование сбрасывало максимизированное окно.
        self.project = Project()
        self.project_store = ProjectStore(Path(__file__).resolve().parent / 'Проекты')
        self.home = None
        self.current_path: Path | None = None
        self.current_name: str | None = None
        self.result: Result | None = None
        # Новая последовательность: Элементы -> Рассчитать -> Оптимизировать.
        self.workflow_stage='elements'
        self.survey=None
        self.selected_link_element=None
        self.link_candidates=[]
        self.selected_link_index=None
        self.highlighted_trunk_index=None
        self._last_line_pick=None
        self._slab_snapshot=None
        self.link_catalog={}
        self._independent_catalog={}
        self._base_result=None
        self._base_project_snapshot=None
        self._last_progress=None
        self._search_progress_counter=0
        self._linked_project_snapshot=None
        self.link_filter_var=tk.StringVar(value='Все среды')
        self.zoom = 1.0
        self.cx = 0.0
        self.cy = 0.0
        self.drag: tuple[int,int,float,float] | None = None
        self.pick_callback = None
        self.pick_cancel_callback = None
        self._auto_job = None
        self._loading_form = False
        self._build_seq = 0
        self._build_queue = queue.SimpleQueue()
        self._build_busy = False
        self._build_pending = None
        self._build_polling = False
        self.search_visual_var=tk.BooleanVar(value=True)
        self.pipe_mode=tk.BooleanVar(value=False)  # простой вид по умолчанию
        self.show_well_guards=tk.BooleanVar(value=False)  # визуализация, не запуск расчёта
        self.show_main_guards=tk.BooleanVar(value=False)  # хребет/центральная плита 1,5 м
        self.vertical_enabled=tk.BooleanVar(value=True)  # показывать разметку пересечений
        self.inspect_mode=tk.BooleanVar(value=False)
        self.inspected_segment=None
        self.inspection_window=None
        self.inspection_text=None
        self.inspection_points=None
        self.show_candidate_points=tk.BooleanVar(value=True)
        self._init_style()
        self._build_controls()
        self.load_project_into_form()
        self.fields["azimuth"].trace_add("write",self._azimuth_changed)
        self.coord_mode.trace_add("write",self._azimuth_changed)
        self.bind("<Escape>",self.cancel_pick)
        self.protocol('WM_DELETE_WINDOW', self.back_to_home)
        self.after(0,self.show_home)

    def _init_style(self):
        self._theme_style = apply_theme(self)

    def _wheel_over_settings(self,event):
        """Прокрутка настроек под указателем мыши, не масштабируя генплан."""
        try:
            widget=self.winfo_containing(event.x_root,event.y_root)
            parent=widget
            while parent is not None:
                if parent==self.settings_scroller:
                    if getattr(event,'num',None)==4:
                        direction=-1
                    elif getattr(event,'num',None)==5:
                        direction=1
                    else:
                        delta=getattr(event,'delta',0)
                        direction=-(1 if delta>0 else -1) if delta else 0
                    if direction:
                        self.settings_scroller.yview_scroll(direction*3,'units')
                    return 'break'
                parent=parent.master if isinstance(parent,tk.Misc) else None
        except (tk.TclError, AttributeError):
            pass
        return None

    def _build_controls(self):
        self.columnconfigure(1,weight=1)
        self.rowconfigure(0,weight=1)
        left=ttk.Frame(self,width=460)
        left.grid(row=0,column=0,sticky="nsw")
        left.grid_propagate(False)
        # The well table lives OUTSIDE the scrolling settings form, so the full
        # 10-well default list stays visible at all times.
        wells_section=ttk.LabelFrame(left,text="1. Скважины (всегда видны)",padding=8)
        wells_section.pack(side="top",fill="x",padx=(5,4),pady=(5,2))
        scroller_area=ttk.Frame(left)
        scroller_area.pack(side="top",fill="both",expand=True)
        outer=tk.Canvas(scroller_area,background=PALETTE["panel"],highlightthickness=0,width=440)
        bar=ttk.Scrollbar(scroller_area,orient="vertical",command=outer.yview)
        outer.configure(yscrollcommand=bar.set)
        outer.pack(side="left",fill="both",expand=True)
        bar.pack(side="right",fill="y")
        form=ttk.Frame(outer,padding=13)
        win=outer.create_window((0,0),window=form,anchor="nw")
        form.bind("<Configure>",lambda e:outer.configure(scrollregion=outer.bbox("all")))
        outer.bind("<Configure>",lambda e:outer.itemconfigure(win,width=e.width))
        self.settings_scroller=outer
        # Tk на Windows передаёт вращение колеса элементу под курсором.
        # Обрабатываем в том числе поля, выпадающие списки и подписи внутри формы.
        # Над 2D-планом колесо по-прежнему изменяет масштаб.
        self.bind_all('<MouseWheel>', self._wheel_over_settings, add='+')
        self.bind_all('<Button-4>', self._wheel_over_settings, add='+')
        self.bind_all('<Button-5>', self._wheel_over_settings, add='+')

        ttk.Label(form,text="ПРОЕКТИРОВАНИЕ ТРУБОПРОВОДОВ",font=("Segoe UI",14,"bold"),foreground=PALETTE["text"]).pack(anchor="w",pady=(2,6))
        ttk.Label(form,text="2D · Выбор центра из 4 позиций · Центральная плита 6×1,75 м",foreground=PALETTE["muted"]).pack(anchor="w",pady=(0,12))
        self.fields: dict[str,tk.StringVar]={}
        base=ttk.LabelFrame(form,text="2. Исходные координаты",padding=10)
        base.pack(fill="x",pady=(0,10))
        for field,label,default in (
            ("first_x","Первая скважина: X", "100"),
            ("first_y","Первая скважина: Y", "100"),
            ("tie_x","Точка врезки: X", "200"),
            ("tie_y","Точка врезки: Y", "90"),
            ("center_x","Центр плиты: X", ""),
            ("center_y","Центр плиты: Y", ""),
            ("azimuth","Азимут НДС, град.", "90"),
            ("fire_x","Пожарный узел: X", "112.75"),
            ("fire_y","Пожарный узел: Y", "53"),
        ):
            self.fields[field]=tk.StringVar(value=default)
            r=ttk.Frame(base);r.pack(fill="x",pady=3)
            ttk.Label(r,text=label,width=24).pack(side="left")
            ttk.Entry(r,textvariable=self.fields[field],width=18).pack(side="right",fill="x",expand=True)
        for title,which in (("Указать первую скважину на плане","first"),
                            ("Указать точку врезки на плане","tie"),
                            ("Закрепить центральный узел на плане","center"),
                            ("Указать пожарный узел на плане","fire")):
            ttk.Button(base,text="⌖  " + title,
                       command=lambda w=which:self.pick_coordinate_pair(w)).pack(fill="x",pady=2)
        ttk.Button(base,text='↺ Точка врезки автоматически — перпендикуляр через центр',command=self.reset_auto_tie).pack(fill='x',pady=3)
        ttk.Label(base,text="Точка выбирается щелчком на правом чертеже, не на спутниковой карте.",
                  foreground=PALETTE["muted"],wraplength=375).pack(anchor="w",pady=(3,8))
        ttk.Label(base,text="Соглашение для координат:").pack(anchor="w",pady=(7,2))
        self.coord_mode=tk.StringVar(value="X — север, Y — восток (геодезическое)")
        self.coord_modes={"X — север, Y — восток (геодезическое)":"x_north",
                          "X — восток, Y — север (декартово)":"x_east"}
        ttk.Combobox(base,textvariable=self.coord_mode,state="readonly",
                     values=list(self.coord_modes.keys()),width=39).pack(fill="x")
        ttk.Label(base,text="Центр выбирается автоматически между двумя концами базового хребта или вручную.\nТочные X/Y центра плиты имеют приоритет; очистите их для автоматического выбора.",foreground=PALETTE["muted"],wraplength=375).pack(anchor="w",pady=(8,0))

        # Fixed lower left pane: the table does not move when settings scroll.
        nr=ttk.Frame(wells_section);nr.pack(fill="x",pady=3)
        ttk.Label(nr,text="Количество (1–100)").pack(side="left")
        self.count_var=tk.StringVar(value="8")
        style_spinbox(tk.Spinbox(nr,from_=1,to=100,textvariable=self.count_var,width=6)).pack(side="left",padx=10)
        ttk.Button(nr,text="Применить",command=self.resize_wells).pack(side="left")
        self.tree=ttk.Treeview(wells_section,columns=("n","kind","status","placement","gas"),show="headings",height=10,selectmode="browse")
        for name,title,width in (("n","№",35),("kind","Тип",132),("status","Статус",118),("placement","Положение",92),("gas","Газ",35)):
            self.tree.heading(name,text=title)
            self.tree.column(name,width=width,anchor="center",stretch=name in ("kind","status"))
        self.tree.pack(fill="x",pady=7)
        self.tree.bind("<Double-1>",self.edit_selected)
        ttk.Button(wells_section,text="Изменить выбранную скважину",command=self.edit_selected).pack(fill="x")
        ttk.Label(wells_section,text="Шаг на оси: 13,5 м; после каждых 4 скважин — 15 м.\nВнеосевые скважины не занимают позицию на оси.",foreground=PALETTE["muted"],wraplength=390).pack(anchor="w",pady=(7,0))

        opt=ttk.LabelFrame(form,text="3. Хребет и подключения",padding=10)
        opt.pack(fill="x",pady=(0,10))
        for field,label,default in (("head_lead","Отступ центра плиты от ряда, м", "12"),
                                    ("offset","Ближняя труба от НДС, м", "12"),
                                    ("lane_gap","Зазор труб хребта, м", "0.25")):
            self.fields[field]=tk.StringVar(value=default)
            row=ttk.Frame(opt);row.pack(fill="x",pady=3)
            ttk.Label(row,text=label,width=28).pack(side="left")
            ttk.Entry(row,textvariable=self.fields[field],width=10).pack(side="right")
        ttk.Label(opt,text='Положение центрального узла').pack(anchor='w',pady=(8,2))
        self.center_position_var=tk.StringVar(value='Автоматически (2 конца хребта)')
        self.center_position_labels={
            'Автоматически (2 конца хребта)':'auto',
            'Слева — сверху':'left_upper',
            'Слева — снизу':'left_lower',
            'Справа — сверху':'right_upper',
            'Справа — снизу':'right_lower',
            'Слева (старый режим)':'left_auto',
        }
        ttk.Combobox(opt,textvariable=self.center_position_var,state='readonly',
                     values=list(self.center_position_labels),width=35).pack(fill='x',pady=(0,4))
        ttk.Label(opt,text='Справа — на том же расстоянии ПОСЛЕ последней осевой скважины. '
                            'Автоматический выбор слева/справа сравнивает сеть скважин и врезки БЕЗ пожарной линии. Пожарный узел не переносит центральную плиту. '
                            'Верх/низ фиксируются до расчёта: по числу внеосевых элементов или ручному ограничению.',
                  wraplength=385,foreground=PALETTE['muted']).pack(anchor='w',pady=(2,5))
        ttk.Label(opt,text='Сторона хребта относительно НДС (ограничение для авто)').pack(anchor='w',pady=(8,2))
        self.trunk_side_var=tk.StringVar(value='Автоматически')
        self.side_labels={'Автоматически':'auto','Положительная':'positive','Отрицательная':'negative'}
        ttk.Combobox(opt,textvariable=self.trunk_side_var,state='readonly',values=list(self.side_labels),width=30).pack(fill='x')
        self.auto_rotate_var=tk.BooleanVar(value=True)
        ttk.Checkbutton(opt,text='Выбирать ориентацию внеосевых скважин по стоимости J (поворот бесплатен)',variable=self.auto_rotate_var).pack(anchor='w',pady=3)
        ttk.Label(opt,text='Стоимостные коэффициенты расчёта J редактируются в кнопке «Детали» над чертежом.',
                  foreground=PALETTE["muted"],wraplength=370).pack(anchor='w',pady=(8,6))
        sep=ttk.LabelFrame(opt,text='4. Условные участки пересечений (только 2D)',padding=7)
        sep.pack(fill='x',pady=(8,6))
        ttk.Label(sep,text='При проходе одной трубы через две соседние предпочтительна общая отметка. При одиночном пересечении хребет и общий коридор защищены от выбора. Физические подъёмы не строятся.',
                  wraplength=350,foreground=PALETTE["muted"]).pack(anchor='w',pady=(1,4))
        self.fields['bridge_approach']=tk.StringVar(value='0.5')
        row=ttk.Frame(sep);row.pack(fill='x',pady=2)
        ttk.Label(row,text='Отступ от пересечения, м',width=29).pack(side='left')
        ttk.Entry(row,textvariable=self.fields['bridge_approach'],width=10).pack(side='right')
        row=ttk.Frame(sep);row.pack(fill='x',pady=2)
        self.fields['branch_plate_offset']=tk.StringVar(value='1')
        ttk.Label(row,text='Отступ плиты отпайки от скважины, м',width=29).pack(side='left')
        ttk.Entry(row,textvariable=self.fields['branch_plate_offset'],width=10).pack(side='right')
        # Для чтения старых проектов поле остаётся в модели, но оно не влияет на 2D.
        self.fields['vertical_clearance']=tk.StringVar(value='0.25')
        ttk.Label(sep,text='Длина участка теперь учитывается вместе с возможной экономией отводов; максимальный размах автоматической группы — 6 м. Физическая высота не рассчитывается.',
                  wraplength=350,foreground=PALETTE["warning"]).pack(anchor='w',pady=(3,1))
        ttk.Label(opt,text="Порядок труб ПОПЕРЁК ХРЕБТА: СППВ → ППД → замер нефти → газ → продуктовая нефть.\nЦентральная плита 6×1,75 м включена в расчёт. Другие МИС пока не моделируются.",foreground=PALETTE["muted"],wraplength=375).pack(anchor="w",pady=(7,4))
        ttk.Label(opt,text="Среда пожарного трубопровода:").pack(anchor="w",pady=(6,1))
        self.shared_corridors_var=tk.BooleanVar(value=True)
        ttk.Checkbutton(opt,text='Пучки внешних линий (сейчас не используются — внешний алгоритм снят)',
                        variable=self.shared_corridors_var,command=self.rebuild).pack(anchor='w',pady=(8,4))
        ttk.Label(opt,text='При включении проверяются варианты общей прокладки. Оставляется только допустимый вариант с меньшей полной стоимостью труб, отводов и плит. Пожарная линия не входит в пучки.',
                  foreground=PALETTE["muted"],wraplength=375).pack(anchor='w',pady=(0,7))
        self.fire_medium_var=tk.StringVar(value="ППД (высокое давление)")
        self.fire_medium_labels={"ППД (высокое давление)":"pressure","СППВ (пресная вода)":"fresh"}
        ttk.Combobox(opt,textvariable=self.fire_medium_var,values=list(self.fire_medium_labels),state="readonly",width=37).pack(fill="x")
        ttk.Label(opt,text="ВНЕШНИЕ ТРАССЫ (v92).\n"
                  "Рассчитать: независимое подключение внеоси и врезки к боку хребта\n"
                  "(на хребтовых плитах / дальнему торцу) или боку ЦП со стороны элемента.\n"
                  "Оптимизировать: общие трубы одной среды и общие плиты 1,2×3.\n"
                  "Пожарная линия самостоятельная. Плиты внешних линий пока равномерно (зазор 3–6 м).\n"
                  "Схема ориентирована по сторонам света: азимут меняет направление оси НДС на чертеже.",
                  foreground=PALETTE["hint"],wraplength=390).pack(anchor="w",pady=(9,4))
        ttk.Label(opt,text="Газ для скважины включается отдельно в её карточке.\nЭто предварительный расчёт без гидравлики и проверки строительных норм.",foreground=PALETTE["muted"],wraplength=370).pack(anchor="w",pady=(8,0))

        actions=ttk.LabelFrame(form,text="5. Расчёт и файлы",padding=10)
        actions.pack(fill="x",pady=(0,10))
        ttk.Button(actions,text="1. Рассчитать — ядро + независимые внешние",style="Accent.TButton",command=self.run_calculation).pack(fill="x",pady=3)
        ttk.Button(actions,text="2. Оптимизировать — общие трубы и плиты",command=self.run_optimization).pack(fill="x",pady=3)
        prog_row=ttk.Frame(actions)
        prog_row.pack(fill='x',pady=(6,2))
        ttk.Label(prog_row,text='Прогресс:').pack(side='left')
        self.calc_progress_var=tk.DoubleVar(value=0)
        self.calc_progress=ttk.Progressbar(prog_row,variable=self.calc_progress_var,
                                           maximum=100,mode='determinate',length=220)
        self.calc_progress.pack(side='left',fill='x',expand=True,padx=(6,0))
        self.calc_progress_label=tk.StringVar(value='0 %')
        ttk.Label(prog_row,textvariable=self.calc_progress_label,width=5).pack(side='left',padx=(6,0))
        ttk.Checkbutton(actions,text="Показывать поиск линий на чертеже",variable=self.search_visual_var).pack(anchor="w",pady=3)
        for text,command in (("Сохранить текущий проект",self.save),
                             ("Сохранить копию (.json)",self.save_copy),
                             ("Импортировать старый проект (.json)",self.open),
                             ("← Вернуться к списку проектов",self.back_to_home),
                             ("Экспорт схемы (.svg)",self.svg),
                             ("Ведомость участков (.csv)",self.csv),
                             ("Ведомость участков пересечений (.csv)",self.export_crossings_csv),
                             ("Предварительная ведомость труб (.csv)",self.materials)):
            ttk.Button(actions,text=text,command=command).pack(fill="x",pady=3)
        ttk.Label(form,text="Прокрутка и колесо мыши по правой схеме: масштаб.\nЛевая кнопка + перетаскивание: перемещение.",foreground=PALETTE["muted"]).pack(anchor="w",pady=8)

        right=ttk.Frame(self,padding=(6,8,12,8))
        right.grid(row=0,column=1,sticky="nsew")
        right.rowconfigure(1,weight=1)
        right.columnconfigure(0,weight=1)
        top=ttk.Frame(right)
        top.grid(row=0,column=0,sticky="ew",pady=(0,7))
        title_row=ttk.Frame(top)
        title_row.pack(fill="x")
        self.header=tk.StringVar(value="Исходные элементы — вид сверху")
        self.compute_status=tk.StringVar(value="")
        ttk.Label(title_row,textvariable=self.header,font=("Segoe UI",13,"bold"),foreground=PALETTE["text"]).pack(side="left")
        ttk.Button(title_row,text="Показать всё",command=self.fit).pack(side="right")
        ttk.Label(title_row,textvariable=self.compute_status,foreground=PALETTE["muted"]).pack(side="right",padx=10)
        ttk.Button(title_row,text="Детали",command=self.show_details).pack(side="right",padx=(0,8))
        ttk.Button(title_row,text="Оптимизировать",command=self.run_optimization).pack(side="right",padx=(0,5))
        ttk.Button(title_row,text="Рассчитать",command=self.run_calculation,style="Accent.TButton").pack(side="right",padx=(0,5))
        ttk.Checkbutton(title_row,text='Режим правки',variable=self.inspect_mode,
                        command=self.toggle_inspection).pack(side='right',padx=(0,9))
        controls_row=ttk.Frame(top)
        controls_row.pack(fill='x',pady=(2,1))
        self.search_state=tk.StringVar(value='Ожидание расчёта · маршруты и плиты не строятся при создании проекта')
        ttk.Label(controls_row,textvariable=self.search_state,foreground=PALETTE['muted']).pack(side='left',padx=(4,8))
        if not hasattr(self,'calc_progress_var'):
            self.calc_progress_var=tk.DoubleVar(value=0)
            self.calc_progress_label=tk.StringVar(value='0 %')
        self.header_progress=ttk.Progressbar(controls_row,variable=self.calc_progress_var,
                                             maximum=100,mode='determinate',length=160)
        self.header_progress.pack(side='right',padx=(8,4))
        ttk.Label(controls_row,textvariable=self.calc_progress_label,
                  foreground=PALETTE['muted']).pack(side='right')
        ttk.Checkbutton(controls_row,text='Показывать участки пересечений',variable=self.vertical_enabled,
                        command=self.rebuild).pack(side='right',padx=(2,10))
        ttk.Checkbutton(controls_row,text="Трубы + отводы + тройники",variable=self.pipe_mode,
                        command=self.draw).pack(side="right",padx=(0,10))
        guards_row=ttk.Frame(top)
        guards_row.pack(fill='x',pady=(0,2))
        ttk.Checkbutton(guards_row,text="Показывать защитные зоны скважин (радиус 5 м)",
                        variable=self.show_well_guards,command=self.draw).pack(side='right',padx=(0,10))
        ttk.Checkbutton(guards_row,text="Зона хребта и центральной плиты (1,5 м)",
                        variable=self.show_main_guards,command=self.draw).pack(side='right',padx=(0,12))
        self.canvas=tk.Canvas(right,background=BACKGROUND,highlightbackground=PALETTE["drawing_border"],highlightthickness=1)
        self.canvas.grid(row=1,column=0,sticky="nsew")
        self.canvas.bind("<Configure>",lambda e:self.draw())
        self.canvas.bind("<MouseWheel>",self.on_zoom)
        self.canvas.bind("<Shift-MouseWheel>",self.on_wheel_pan)
        self.canvas.bind("<Shift-Button-4>",lambda e:self.on_wheel_pan(e,-1))
        self.canvas.bind("<Shift-Button-5>",lambda e:self.on_wheel_pan(e,1))
        self.canvas.bind("<Button-4>",lambda e:self._zoom(e,1.10))
        self.canvas.bind("<Button-5>",lambda e:self._zoom(e,1/1.1))
        self.canvas.bind("<ButtonPress-1>",self.on_canvas_press)
        self.canvas.bind("<B1-Motion>",self.pan)
        self.canvas.bind("<Motion>",self.on_canvas_move)
        self.pick_hint = tk.StringVar(value="Масштаб — колесо мыши; перемещение — перетаскивание. Север сверху.")
        ttk.Label(top,textvariable=self.pick_hint,foreground=PALETTE["hint"]).pack(fill="x",anchor="w",pady=(4,0))
        info=ttk.LabelFrame(right,text="Геометрическая длина · предварительные расчёты",padding=8)
        info.grid(row=2,column=0,sticky="ew",pady=(8,0))
        self.summary=tk.StringVar(value="")
        ttk.Label(info,textvariable=self.summary,justify="left",wraplength=950).pack(anchor="w")
        self.warnings=tk.Text(right,height=5,wrap="word",font=("Segoe UI",9),
                              foreground=PALETTE["muted"],background=PALETTE["input"],
                              insertbackground=PALETTE["text"],borderwidth=0,
                              highlightthickness=1, highlightbackground=PALETTE["border"],
                              padx=11,pady=9,spacing1=2)
        self.warnings.grid(row=3,column=0,sticky="ew",pady=(6,0))
        self.warnings.config(state="disabled")


    def toggle_inspection(self):
        """Режим выбора маршрутов: пока это анализ, не ручное изменение координат."""
        self.inspected_segment=None
        self.inspection_points=None
        if self.inspect_mode.get():
            self.canvas.configure(cursor='hand2')
            self.pick_hint.set('РЕЖИМ ПРАВКИ: нажмите на линию для просмотра четырёх выходов и возможных точек подключения; '
                               'Shift + перетаскивание — перемещение чертежа.')
        else:
            self.canvas.configure(cursor='')
            self.pick_hint.set('Режим правки выключен. Перетаскивайте план для перемещения.')
        self.draw()

    def select_route_at(self,x,y):
        """Геометрический выбор труб по расстоянию на ЭКРАНЕ, не по Z-порядку."""
        if self.result is None:return None
        closest=None
        best_distance=max(10.,min(18.,self.zoom*0.30))
        for i,seg in enumerate(self.result.segments):
            ax,ay=self.to_px(seg.a)
            bx,by=self.to_px(seg.b)
            vx,vy=bx-ax,by-ay
            denom=vx*vx+vy*vy
            if denom<1e-10:continue
            t=max(0.,min(1.,((x-ax)*vx+(y-ay)*vy)/denom))
            d=math.hypot(x-(ax+t*vx),y-(ay+t*vy))
            if d < best_distance:
                best_distance=d
                closest=i
        if closest is not None:
            self.inspected_segment=closest
            self.inspection_points=candidate_points(self.result,closest)
            self.draw()
            self._show_route_inspection(closest)
        else:
            self.pick_hint.set('Нет трубы рядом с указателем. Увеличьте масштаб или щёлкните ближе к линии.')
        return closest

    def _show_route_inspection(self,index):
        """Показывать факты и сохранённые результаты расчёта, не выдуманные причины."""
        if self.result is None:return
        points=self.inspection_points or candidate_points(self.result,index)
        report=inspect_segment(self.result,index)+format_points(points)
        window=self.inspection_window
        if window is None or not window.winfo_exists():
            window=tk.Toplevel(self)
            window.title('Режим правки — анализ выбранной трубы')
            window.geometry('820x720')
            window.minsize(560,430)
            window.configure(background=PALETTE['panel'])
            frame=ttk.Frame(window,padding=12)
            frame.pack(fill='both',expand=True)
            ttk.Label(frame,text='Почему выбран этот маршрут',
                      font=('Segoe UI',13,'bold')).pack(anchor='w',pady=(0,5))
            ttk.Label(frame,text='Это диагностический режим. Он показывает реальные сравнения, '
                      'но пока не меняет маршрут вручную.',wraplength=760,
                      foreground=PALETTE['muted']).pack(anchor='w',pady=(0,10))
            area=ttk.Frame(frame)
            area.pack(fill='both',expand=True)
            scrollbar=ttk.Scrollbar(area)
            scrollbar.pack(side='right',fill='y')
            field=tk.Text(area,wrap='word',font=('Consolas',10),
                          background=PALETTE['input'],foreground=PALETTE['text'],
                          insertbackground=PALETTE['text'],padx=12,pady=12,
                          yscrollcommand=scrollbar.set)
            field.pack(side='left',fill='both',expand=True)
            scrollbar.configure(command=field.yview)
            bottom=ttk.Frame(frame)
            bottom.pack(fill='x',pady=(8,0))
            ttk.Button(bottom,text='Скопировать отчёт',
                       command=lambda:self._copy_route_report()).pack(side='left')
            ttk.Checkbutton(bottom,text='Показывать все точки на плане',
                            variable=self.show_candidate_points,
                            command=self.draw).pack(side='left',padx=18)
            ttk.Button(bottom,text='Закрыть',command=window.destroy).pack(side='right')
            self.inspection_window=window
            self.inspection_text=field
            window.protocol('WM_DELETE_WINDOW',window.destroy)
        else:
            window.lift()
        field=self.inspection_text
        if field is not None and field.winfo_exists():
            field.configure(state='normal')
            field.delete('1.0','end')
            field.insert('1.0',report)
            field.configure(state='disabled')
            field.yview_moveto(0)

    def _copy_route_report(self):
        if self.inspection_text is not None and self.inspection_text.winfo_exists():
            self.clipboard_clear()
            self.clipboard_append(self.inspection_text.get('1.0','end-1c'))

    def show_details(self):
        # Справка и рабочие настройки оценки J, сохраняемые в проекте.
        p=self.project
        dialog=tk.Toplevel(self)
        dialog.title('Детали — правила, ограничения и стоимость')
        dialog.geometry('900x760')
        dialog.minsize(740,600)
        dialog.configure(bg=PALETTE['panel'])
        frame=ttk.Frame(dialog,padding=12)
        frame.pack(fill='both',expand=True)
        notebook=ttk.Notebook(frame)
        notebook.pack(fill='both',expand=True)
        rule_tab=ttk.Frame(notebook,padding=8)
        costs_tab=ttk.Frame(notebook,padding=12)
        glossary_tab=ttk.Frame(notebook,padding=12)
        actual_tab=ttk.Frame(notebook,padding=12)
        notebook.add(rule_tab,text='Правила и ограничения')
        notebook.add(costs_tab,text='Стоимость и настройки')
        notebook.add(glossary_tab,text='Словарь и этапы')
        notebook.add(actual_tab,text='Фактические подключения')
        # Not a generic description: exact selected coordinates and the
        # counters/rejections RECORDED during the current completed run.
        from network_integrity import format_actual_connection_report
        if self.result is not None:
            actual_report=format_actual_connection_report(self.result)
        else:
            actual_report=('Расчёт ещё не завершён. Нажмите «Рассчитать», '
                           'затем при необходимости «Оптимизировать» и заново откройте «Детали».')
        ttk.Label(actual_tab,text='Проверка связи каждой скважины с центральной магистралью',
                  foreground=PALETTE['muted'],wraplength=800).pack(anchor='w',pady=(0,9))
        actual_frame=ttk.Frame(actual_tab)
        actual_frame.pack(fill='both',expand=True)
        actual_scroll=ttk.Scrollbar(actual_frame)
        actual_scroll.pack(side='right',fill='y')
        actual_text=tk.Text(actual_frame,wrap='none',font=('Consolas',10),
                            background=PALETTE['input'],foreground=PALETTE['text'],
                            padx=10,pady=10,yscrollcommand=actual_scroll.set)
        actual_text.pack(side='left',fill='both',expand=True)
        actual_scroll.configure(command=actual_text.yview)
        actual_text.insert('1.0',actual_report)
        actual_text.configure(state='disabled')
        ttk.Button(actual_tab,text='Копировать фактический протокол',
                   command=lambda:(self.clipboard_clear(),self.clipboard_append(actual_report))).pack(anchor='w',pady=(8,0))
        glossary_text=tk.Text(glossary_tab,wrap='word',font=('Segoe UI',11),
                              background=PALETTE['input'],foreground=PALETTE['text'],
                              padx=14,pady=12)
        glossary_text.pack(fill='both',expand=True)
        glossary_text.insert('1.0',
            'СЛОВАРЬ ПРОЕКТА\n\n'
            'ЭЛЕМЕНТ — узел, к которому подводятся трубопроводы. '
            'Может содержать входы, выходы либо одновременно и входы, и выходы. '
            'Примеры: скважина, точка врезки, пожарный узел.\n\n'
            'ЛИНИЯ — физический трубопровод между входом и выходом. '
            'Отвод делит её для чертежа, но не разрывает как маршрут.\n\n'
            'МАРШРУТ — геометрия независимой трубы от начального элемента к разрешённому порту.\n\n'
            'КАНДИДАТ — проверяемый вариант геометрии. Порты хребта/центральной плиты '
            'представлены конечным перечнем допустимых точек.\n\n'
            'ПУЧОК — совместная прокладка разных физических труб на общих опорах. '
            'Одна среда может получить общий участок через реальный тройник.\n\n'
            'ПЛИТА — основание для труб и фасонных элементов.\n\n'
            'ПОСЛЕДОВАТЕЛЬНОСТЬ\n\n'
            '1. Элементы: разместить скважины, ось НДС, врезку и пожарный узел.\n'
            '2. Рассчитать: осевое ядро + независимые внешние трассы '
            '(внеось и врезка — каждая сама по себе, с пулом вариантов); '
            'снимок JSON в «Снимки_расчёта».\n'
            '3. Оптимизировать: комбинации запомненных путей — общая труба '
            'одной среды и общие плиты 1,2×3 оплачиваются один раз.\n\n'
            'ТОЧНОСТЬ ПОИСКА\n\n'
            'Для непрерывной геометрии математически бесконечно много маршрутов. '
            'Используется конечная сетка с шагом 1 м, точными координатами '
            'разрешённых портов и геометрическими координатами препятствий; '
            'число дополнительных проверок ограничено. '
            'Поэтому найденный вариант НЕ является доказанным глобальным минимумом. '
            'Итоги и статистика проверок сохраняются в диагностике.')
        glossary_text.configure(state='disabled')
        help_text=tk.Text(rule_tab,wrap='word',font=('Segoe UI',10),
                          background=PALETTE['input'],foreground=PALETTE['text'],
                          borderwidth=0,highlightthickness=1,
                          highlightbackground=PALETTE['border'],padx=12,pady=12)
        scrollbar=ttk.Scrollbar(rule_tab,command=help_text.yview)
        help_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y')
        help_text.pack(side='left',fill='both',expand=True)
        rules='''
ПРАВИЛА КОМПОНОВКИ · ХРЕБЕТ v93c

1. ХРЕБЕТ. Уже на первом этапе формируется неизменяемая базовая часть между двумя возможными центральными плитами; сторона выбирается по большинству внеосевых физических элементов с учётом ручных ограничений. Параллелен оси НДС. Пять позиций по фиксированному порядку, но строятся только востребованные магистрали. Хребет одним торцом примыкает к центральной плите. Подключение боковой ветви к нему — тройник, даже в конечной точке (выход заглушён). Хребтовая труба является последним кандидатом для условного подъёма при пересечении.

2. ОСЕВЫЕ СКВАЖИНЫ. От каждой скважины идут только требуемые независимые отпайки до хребта. Их нельзя объединять между собой и считать транзитными магистралями. Пересечения и соединения с хребтовыми линиями разрешены. При пересечении отпаечная труба ВСЕГДА считается верхней; хребтовая остаётся на своём уровне. Соединение с трубой той же среды оформляется тройником. Пересечение с чужой нехребтовой линией требует отдельной проверки.

ПРАВИЛО внешних транзитов: 0,30 м — межосевое расстояние между параллельными внешними ТРАНЗИТНЫМИ трубами. Хребет сохраняет отдельный зазор 0,25 м: пять ниток с 0,30 м не помещаются на плиту 1,75 м. Плита транзита имеет ширину 1,2 м. Проверка пучков выполняется при нажатии «Оптимизировать», с сохранением всех правил допустимости.

3. ВНЕОСЕВЫЕ СКВАЖИНЫ. Предпочтительна наиболее экономичная допустимая трасса к своей среде хребта или плиты. Для пресной воды СППВ, пластовой воды ППД и продуктовой нефти запрещены торцевые входы в плиту (кроме самостоятельного пожарного подключения). Транзитные линии могут подключаться к боковой (длинной) стороне плиты; выбирается подходящий участок её 6-метровой магистрали. «Рассчитать» ищет независимые трассы, а «Оптимизировать» рассматривает новые допустимые объединения и перестройки с полной переоценкой стоимости. Для каждой полной конфигурации суммируется цена метража труб, отводов и фактических плит 1,2×3 м, с необязательной ценой тройников. Пересечения отображаются, но их условная стоимость пока не учитывается. Врезка к плите и к хребту сравнивается после построения сети; при включённом режиме оценивается и возможное совместное прохождение со скважинными транзитами.

4. ПОЖАРНЫЙ УЗЕЛ. Трубопровод полностью самостоятельный: запрещено включать его в общие пучки и использовать как направляющую для транзитных веток, даже когда среды различаются. Близкое параллельное следование более 3 м на расстоянии меньше 1 м от скважинного транзита запрещено. Пожарный вывод не даёт бонуса сборки пучка. Подвод выполняется с торцевой стороны плиты, не сбоку. Физическое соединение с наружным торцом центральной плиты ИЛИ дальним свободным торцом хребта той же среды, смотря что ближе по допустимому маршруту. Узел — точка без вариантов входа. Первым участок стремится идти по фантомному продолжению торца, затем поворачивает к узлу. В первую очередь резервируется кратчайший прямой выход, чтобы ранее построенные ветки СППВ/ППД не заставляли пожарный трубопровод делать лишние обходы. При конфликте с ним смещается транзитная ветвь при соблюдении защитных зон.

5. ТОЧКА ВРЕЗКИ. Подводит/отводит только продуктовую нефть. Подключение допускается ТОЛЬКО через боковую грань центральной плиты или к продуктовому хребту; торцы плиты запрещены. В первую очередь рассматривается ближайший физически доступный тройник продуктового хребта. Если локальная оценка выбирает далёкую точку, строится вторая ПОЛНАЯ сеть с ближайшим допустимым тройником, включая пожарный вывод; сравнивается итоговая J, без повторного начисления стоимости отводов. Возможность короткого безопасного бокового подключения сохраняется.

6. ЦЕНТРАЛЬНАЯ ПЛИТА. Размер 6 × 1,75 м. Только нужные среды, полные шестиметровые трубы, свободные концы закрываются заглушками. Хребет подключается к внутреннему торцу; пожарная линия выходит из наружного торца плиты или дальнего свободного торца хребта. Пресная вода СППВ, пластовая вода ППД и продуктовая нефть подключаются сбоку либо к хребту, НЕ к торцам плиты. Исключение: самостоятельный пожарный трубопровод. Прочие внеосевые транзиты подходят к боковому порту или к хребту, торцевые вводы недопустимы. Подводимые трубы условно проходят сверху, магистрали плиты не поднимаются.

7. ПЛИТЫ ОСЕВЫХ ПОДКЛЮЧЕНИЙ. Размер 3 × 1,75 м, расположены под хребтовыми трубами в местах подключения скважин с оси НДС. Границы по ширине совпадают с центральной плитой; продольная привязка определяется фактическими точками подключения. В пределах одной плиты проходят только используемые в этом створе хребтовые магистрали, непрерывно на всю длину 3 м. Отпайки присоединяются через тройники, магистрали на плите остаются нижними при пересечении. Последняя нитка продолжается до края плиты и закрывается заглушкой. Если подключения одной скважины разнесены более чем на 3 м, нужны отдельные плиты.

8. ПЛИТЫ ПОД ОТПАЕЧНЫМИ ЛИНИЯМИ. Размер 1,2 × 6 м. Ставятся вдоль отпаячных линий осевых скважин; длинная сторона параллельна трубе. Ближняя к скважине кромка отступает от скважины на настраиваемое расстояние (по умолчанию 1 м). Если несколько ниток идут единым коридором, ставится одна плита по центру коридора.

9. ПРОМЕЖУТОЧНЫЕ ХРЕБТОВЫЕ ПЛИТЫ. Размеры 1,75 × 3 м и 1,75 × 6 м. Ставятся по центру между соседними соединительными хребтовыми плитами: если зазор между их краями меньше 5 м — не ставятся; если зазор от 5 до 11 м включительно — ставится плита 1,75 × 3 м; если зазор больше 11 м — плита 1,75 × 6 м.

10. ЛОКАЛЬНЫЕ ПЛИТЫ 1,2 × 3 м. Ставятся у точки врезки, пожарного узла и внеосевых скважин. Обычно длинная сторона параллельна линии. Если последний участок короче ширины плиты 1,2 м и поворот помещается внутри неё, длинная сторона обязательно ориентируется вдоль предыдущего участка. Размеры плиты не сокращаются. Когда плита попадает на пучок, она центрируется по всем физически помещающимся ниткам; зазор и размеры труб учитываются. Для внеосевых скважин плита ставится на расстоянии 1 м от выхода трубы; первый прямой участок удлинён до 4 м, чтобы обеспечить место под плиту 3 м.

11. ПЛИТЫ ТРАНЗИТНЫХ УЧАСТКОВ. После фиксации угловых плит свободные промежутки заполняются заново модулями 1,2 × 3 м, с одинаковыми допустимыми зазорами 2–4 м между закреплёнными соседями и регулярными плитами. Участок разбивается не по названию трубопровода, а по фактической геометрии. Если в пучок входит или из него выходит новая труба, серия плит НЕ начинается заново: в каждой точке опоры вычисляются фактически проходящие там нитки и поперечный центр пучка. Существующие плиты любого направления исключают наложение. Короткие отрезки, которые физически нельзя заполнить полноразмерной плитой, фиксируются как ограничение; допускаются меньшие краевые отступы, но не недопустимые расстояния между плитами.

12. ОБЯЗАТЕЛЬНЫЕ ОПОРЫ УЗЛОВ. Каждое пересечение разных сред, тройник и поворотный отвод находятся на плите или не далее 0,25 м от неё. Сначала фиксируются существующие опоры узлов, затем ближайшая регулярная плита при необходимости сдвигается строго по направлению своей трубы и фиксируется, после чего все остальные регулярные плиты удаляются и расставляются заново в свободных промежутках. У поворотной плиты предпочтительно совпадение короткой грани с местом поворота вместо выступа за угол. Небольшие транзитные плиты 1,2 × 3 м не имеют права пересекать хребтовые трубы. Если под внешним тройником или пересечением у хребта отсутствует опора, используется стандартная соединительная хребтовая плита 3 × 1,75 м. Специальные плиты не сдвигаются. Если строгий шаг 2–4 м вместе с неподвижными плитами невозможен, приоритет у поддержки фасонных узлов, и конфликт явно учитывается.

13. ПОДКЛЮЧЕНИЕ ТРАНЗИТА К ХРЕБТУ. При возможности последний вертикальный участок и предыдущее горизонтальное плечо сдвигаются вместе к ближайшей хребтовой плите, без нового отвода. Если вход только прямой и такой перенос создаст лишние отводы, добавляется соединительная хребтовая плита под тройником, а не транзитная плита поперёк хребта.

14. ЗАПРЕТ ЧЕТВЕРНИКОВ. Узел одной среды может содержать не более трёх направлений соединения. При обнаружении четырёхстороннего соединения подводимая ветка переносится к ближайшему свободному месту магистрали с отдельным тройником. Если перенос нарушает ограничения, выдаётся ошибка, а не рисуется фиктивный четырёхпортовый тройник.

15. РАБОЧЕЕ ОКНО. После создания или открытия проекта рабочий интерфейс разворачивается на весь экран.

16. РЕЖИМ ПРАВКИ (v59). Кнопка рядом с «Детали»: клик по трубе выделяет участок и открывает диагностическую карточку. В ней показаны геометрия, текущая J и записанные реальные кандидатные подключения, в том числе варианты, проверенные до объединения в пучок и сравнения ориентаций. Непроверенные варианты не объявляются отклонёнными. Пока режим предназначен для диагностики, перемещение труб вручную не реализовано.

17. ДВА ЭТАПА (v92). «Рассчитать»: осевое ядро, затем для каждого внешнего элемента независимо — допустимые точки (бок хребта на хребтовых плитах / дальний торец / бок ЦП со стороны элемента), самые дешёвые пути, пул вариантов; плиты 1,2×3 равномерно с зазором 3–6 м; линии разных элементов не объединяются. «Оптимизировать»: перебор запомненных путей; одна среда на одну трубу и общие плиты делят стоимость.

18. ЗАЩИТНЫЕ ЗОНЫ (v68). Включите галочку «Показывать защитные зоны скважин (радиус 5 м)», чтобы видеть окружности вокруг всех скважин на любом этапе. Включение не пересчитывает линии. Транзитным линиям запрещено идти вдоль хребта со стороны НДС (между скважинами и хребтом); при поиске обхода учитываются 5-метровые охранные окружности. Фиксированные поперечные отпайки осевых скважин и базовый хребет сохраняются.

19. ВНЕОСЕВЫЕ СКВАЖИНЫ (v70). Каждый выход может смотреть в U+, U−, V+ или V−, если не зафиксирована ориентация. Для каждой среды разрешены только свои трубопроводы: боковой порт центральной плиты со стороны самой скважины (шаг 0,1 м), поддерживаемые плитами точки собственной хребтовой трубы с внешней стороны относительно НДС (шаг 0,1 м) или её свободный дальний торец. Вход в торцы центральной плиты запрещён для ВСЕХ внеосевых скважин, включая газовые. Разворот на 180° вдоль оси одной трубы недопустим. Пучки независимых труб допускаются. Объединение физических ниток одной среды через тройник рассматривается только между однотипными скважинами и лишь при уменьшении полной стоимости J. Пожарный узел остаётся отдельным. Для объекта со стороны НДС боковой вход в хребет вообще не перебирается, поскольку он потребовал бы подключения с запрещённой внутренней стороны либо обхода на противоположную сторону; остаются разрешённые боковые порты центральной плиты и дальний торец хребта.

ОГРАНИЧЕНИЯ И ПРОТИВОРЕЧИЯ

• Осевые отпайки могут пересекать хребтовые нити: при этом они условно проходят поверх хребта. Реальная высота перехода, материалы и монтажная пригодность не рассчитываются. Пересечения осевых отпайков с НЕхребтовыми линиями остаются предметом диагностики.
• Запрещены новые касания концов труб РАЗНЫХ сред, которые могли бы ошибочно имитировать тройник.
• Защитные зоны скважин: 5 м для чужих коммуникаций. Условные исключения для уже существующего хребта учитываются отдельно.
• Не рассчитываются гидравлика, монтажные радиусы, высотные переходы, нормативы и реальная стоимость/спецификация плит; учитывается только их эскизная геометрия и количество.
• Подбор основан на конечном наборе проверенных кандидатов, а не на доказанном математическом глобальном минимуме.
• Условия для экономической модели задаются ниже. Все коэффициенты условные; никакие рыночные расценки не подставляются.
'''
        help_text.insert('1.0',rules.strip())
        help_text.configure(state='disabled')

        cost_labels=[
          ('pipe_cost_per_m','Трубопровод, ₽/м'),
          ('elbow_cost','Отвод 90°, ₽/шт.'),
          ('plate_cost','Плита 1,2 × 3 м, ₽/шт.'),
          ('tee_cost','Тройник, ₽/шт. (по умолчанию 6000)')]
        ttk.Label(costs_tab,text='Редактируемая стоимость J, ₽',
                  font=('Segoe UI',13,'bold')).pack(anchor='w',pady=(0,10))
        value_vars={}
        for key,label in cost_labels:
            row=ttk.Frame(costs_tab)
            row.pack(fill='x',pady=6)
            ttk.Label(row,text=label,width=53).pack(side='left')
            value_vars[key]=tk.StringVar(value=f'{getattr(p,key):g}')
            ttk.Entry(row,textvariable=value_vars[key],width=14).pack(side='right')
        ttk.Label(costs_tab,text='Алгоритм сравнивает каждую проверенную допустимую сеть по стоимости J.\n\n'
                  'J = метры труб × ₽/м + отводы × ₽/шт. + реальные плиты 1,2×3 м × ₽/шт. '
                  '+ тройники × настраиваемую цену (по умолчанию 6000 ₽). '
                  'Другие типоразмеры плит не оценены: для них нет назначенной цены. '
                  'Пересечения отображаются без условного денежного штрафа. '
                  'Пучки выигрывают только за счёт физического сокращения труб, отводов или количества плит.',wraplength=660,
                  foreground=PALETTE['muted']).pack(anchor='w',pady=(8,14))
        ttk.Label(costs_tab,text='Прочие текущие параметры',font=('Segoe UI',12,'bold')).pack(anchor='w')
        current=(f'Положение: {self.center_position_var.get()}   |   Сторона: {self.trunk_side_var.get()}\\n'
                 f'Центр плиты: {p.head_lead:g} м от ряда   |   До ближайшей трубы: {p.offset:g} м\\n'
                 f'Зазор между трубами: {p.lane_gap:g} м   |   Пожарная среда: {p.fire_medium}\\n'
                 f'Пучки: {"сравниваются" if p.shared_corridors else "выключены"}   |   2D-маркеры: {"да" if p.auto_overpasses else "нет"}')
        ttk.Label(costs_tab,text=current,wraplength=660,foreground=PALETTE['hint']).pack(anchor='w',pady=8)
        status=tk.StringVar(value='')
        ttk.Label(costs_tab,textvariable=status,foreground=PALETTE['hint'],wraplength=700).pack(anchor='w',pady=(8,3))
        def apply():
            old={key:getattr(p,key) for key,_ in cost_labels}
            try:
                for key,_ in cost_labels:
                    value=float(value_vars[key].get().replace(',','.'))
                    setattr(p,key,value)
                p.validate()
                self.rebuild()
                status.set('Коэффициенты применены. Для новой сети нажмите «Рассчитать», затем «Оптимизировать».')
            except Exception as exc:
                for key,value in old.items():setattr(p,key,value)
                messagebox.showerror('Стоимость',str(exc),parent=dialog)
        ttk.Button(costs_tab,text='Применить и пересчитать',command=apply,style='Accent.TButton').pack(anchor='w',pady=10)
        ttk.Button(frame,text='Закрыть',command=dialog.destroy).pack(anchor='e',pady=(9,0))

    def load_project_into_form(self):
        self._loading_form=True
        p=self.project
        self.shared_corridors_var.set(p.shared_corridors)
        self.vertical_enabled.set(p.auto_overpasses)
        self.auto_rotate_var.set(p.auto_rotate)
        self.trunk_side_var.set(next((k for k,v in self.side_labels.items() if v==p.trunk_side),'Автоматически'))
        self.center_position_var.set(next((k for k,v in self.center_position_labels.items() if v==p.center_position),
                                          'Автоматически (2 конца хребта)'))
        self.fire_medium_var.set(next((k for k,v in self.fire_medium_labels.items() if v==p.fire_medium), "ППД (высокое давление)"))
        self.coord_mode.set(next((k for k,v in self.coord_modes.items() if v==p.coordinate_mode),
                                 "X — север, Y — восток (геодезическое)"))
        for key,var in self.fields.items():
            var.set(fmt(getattr(p,key)))
        self.count_var.set(str(len(p.wells)))
        self.render_table()
        self._loading_form=False

    def render_table(self):
        # Up to 12 rows visible at once; for larger projects the list itself
        # scrolls independently of the settings (rather than being hidden).
        self.tree.configure(height=max(10,min(12,len(self.project.wells))))
        self.tree.delete(*self.tree.get_children())
        axis_no=0
        for well in self.project.wells:
            if well.on_axis:
                placement=f"НДС +{axial_u(axis_no):g} м"
                axis_no+=1
            else:
                placement="Вне оси"
            self.tree.insert("", "end", iid=str(well.number),values=(well.number,KIND_RU[well.kind],STATUS_RU[well.status],placement,"Да" if well.gas else "—"))

    def resize_wells(self):
        try:
            n=int(self.count_var.get())
            if not 1 <= n <= 100:
                raise ValueError("Нужно выбрать число от 1 до 100")
            old=self.project.wells
            self.project.wells=old[:n]+[Well(i) for i in range(len(old)+1,n+1)]
            self.render_table()
            self.rebuild()
        except Exception as e:
            messagebox.showerror("Количество скважин",str(e),parent=self)

    def edit_selected(self,event=None):
        selection=self.tree.selection()
        if not selection:
            messagebox.showinfo("Выбор скважины","Сначала выберите строку в таблице.",parent=self)
            return
        idx=int(selection[0])-1
        def saved(w):
            self.project.wells[idx]=w
            self.render_table()
            self.tree.selection_set(str(w.number))
            self.rebuild()
        WellDialog(self,self.project.wells[idx],saved)

    def read_form(self):
        p=self.project
        old_tie=(p.tie_x,p.tie_y)
        old_fire=(p.fire_x,p.fire_y)
        for key in self.fields:
            optional=key in ('center_x','center_y')
            setattr(p,key,num(self.fields[key].get(),key,optional))
        if any(not math.isclose(a,b,rel_tol=0,abs_tol=1e-6) for a,b in zip((p.tie_x,p.tie_y),old_tie)):
            p.tie_confirmed=True
        if any(not math.isclose(a,b,rel_tol=0,abs_tol=1e-6) for a,b in zip((p.fire_x,p.fire_y),old_fire)):
            p.fire_confirmed=True
        p.coordinate_mode=self.coord_modes[self.coord_mode.get()]
        p.fire_medium=self.fire_medium_labels[self.fire_medium_var.get()]
        p.shared_corridors=self.shared_corridors_var.get()
        p.auto_overpasses=self.vertical_enabled.get()
        p.trunk_side=self.side_labels[self.trunk_side_var.get()]
        p.center_position=self.center_position_labels[self.center_position_var.get()]
        p.auto_rotate=self.auto_rotate_var.get()
        p.validate()

    def reset_auto_tie(self):
        self.project.tie_confirmed=False
        self.rebuild()

    def _commit_build(self, result):
        # The selected terminal reaches may be SHORTER than the provisional
        # trunk shown at project creation. The connected part must still be
        # exactly collinear with (and contained in) its original trunk.
        if self.survey is not None:
            if result.trunk_side != self.survey.trunk_side:
                raise ValueError('Расчёт попытался перенести базовый хребет на другую сторону НДС.')
            trunk_kinds={'Хребет','Перспективный хребет','Перспективное продление хребта'}
            for s in result.segments:
                if s.purpose not in trunk_kinds:continue
                lo,hi=sorted((s.a.u,s.b.u))
                candidates=[b for b in self.survey.trunk_segments
                            if b.medium==s.medium and b.phase==s.phase
                            and abs(b.a.v-s.a.v)<1e-5 and abs(b.b.v-s.a.v)<1e-5]
                if not any(min(b.a.u,b.b.u)<=lo+1e-5 and
                           hi<=max(b.a.u,b.b.u)+1e-5 for b in candidates):
                    raise ValueError('После отсечения незадействованных концов хребет '
                                     'оказался вне первоначального положения: '+MEDIA_RU[s.medium])
        self.inspected_segment=None
        self.result=result
        self._slab_snapshot=list(result.slabs)
        self.workflow_stage='plates'
        self.compute_status.set('Найден маршрут · оптимальность не доказана')
        self.search_state.set(f'Физическая сеть {result.metrics.get("pipe_m",0.):.1f} м · стоимость {result.metrics.get("optimized_score",0.):,.0f} ₽')
        if self.inspection_text is not None:
            try:
                if self.inspection_text.winfo_exists():
                    self.inspection_text.configure(state='normal')
                    self.inspection_text.delete('1.0','end')
                    self.inspection_text.insert('1.0','Сеть пересчитана. Щёлкните по линии на обновлённом плане.')
                    self.inspection_text.configure(state='disabled')
            except tk.TclError:
                pass
        if self.project.auto_rotate:
            selected={wp.well.number:wp.orientation
                      for wp in result.wells if not wp.well.on_axis and not wp.well.rotation_locked}
            for well in self.project.wells:
                if well.number in selected:
                    # Не превращаем автоматический выбор в фиксированный.
                    well.orientation=selected[well.number]
        if not self.project.tie_confirmed:
            self.project.tie_x,self.project.tie_y=result.frame.xy(result.tie)
            self.fields['tie_x'].set(fmt(self.project.tie_x))
            self.fields['tie_y'].set(fmt(self.project.tie_y))
        if self._base_result is not None:
            self._base_project_snapshot=asdict(self.project)
        self.fit()
        self._update_summary()
        if self.current_path is not None:
            self.project_store.save(self.current_path,self.project,self.current_name)

    def _start_build_worker(self, job):
        """Full geometry/cost search runs off the Tk thread."""
        self._build_busy=True
        seq=job[0]; project_snapshot=job[1]; project_path=job[2]; mode=job[3]
        survey_snapshot=job[4]; base_snapshot=job[5]; catalogue_snapshot=job[6]
        name_hint=job[7] if len(job)>7 else (self.current_name or 'calc')
        self._set_calc_progress(0, 'Запуск…')
        def worker():
            from time import monotonic
            last_report=[0.]
            def progress(event,**info):
                now=monotonic()
                important=event in ('stage','source_done','best_network') or 'percent' in info
                if not important and now-last_report[0]<.12:return
                last_report[0]=now
                self._build_queue.put((seq,project_path,{'event':event,**info},'__progress__'))
            try:
                from calc_optimize import run_calculate, run_optimize
                if mode=='calculate':
                    app_root=Path(__file__).resolve().parent
                    result,catalogue,stats=run_calculate(
                        project_snapshot,survey_snapshot,progress=progress,
                        app_root=app_root,name_hint=str(name_hint or 'calc'))
                    payload=(result,catalogue,stats)
                    self._build_queue.put((seq,project_path,payload,'__initial_done__'))
                else:
                    app_root=Path(__file__).resolve().parent
                    result=run_optimize(project_snapshot,survey_snapshot,catalogue_snapshot,
                                        base_snapshot,progress=progress,
                                        app_root=app_root,name_hint=str(name_hint or 'opt'))
                    self._build_queue.put((seq,project_path,result,None))
            except Exception as exc:
                self._build_queue.put((seq,project_path,None,exc))
        threading.Thread(target=worker,daemon=True,name='wellpad-calc-optimize').start()
        if not self._build_polling:
            self._build_polling=True
            self.after(80,self._poll_build_results)

    def _set_calc_progress(self, percent, label=None):
        pct=max(0.0,min(100.0,float(percent)))
        if hasattr(self,'calc_progress_var'):
            self.calc_progress_var.set(pct)
        if hasattr(self,'calc_progress_label'):
            self.calc_progress_label.set(f'{pct:.0f} %')
        if label:
            self.search_state.set(label)
            self.compute_status.set(label[:80])

    def _paint_search_progress(self,info):
        """Interactive transient overlay: visited point and tested path, not a pipe."""
        event=info.get('event','')
        percent=info.get('percent')
        if event=='search_step':
            label=f"Поиск {info.get('source','')} · {info.get('medium','')} · узлов: {info.get('explored',0)}"
        elif event=='route_candidate':
            label=f"Вариант {info.get('source','')} · путей {info.get('candidate',0)}"
        elif event=='optimization':
            label=(f"Оптимизация {info.get('candidate',0)}/{info.get('total',0)} · "
                   f"лучшая смета: {info.get('best',0):,.0f} ₽")
        elif event=='best_network':
            label=f"{info.get('message','Улучшение')} · {info.get('cost',0):,.0f} ₽"
        elif event=='source_done':
            label=f"Готово {info.get('source','')} · вариантов {info.get('candidate',0)}"
        else:
            label=info.get('message','Проверка ограничений')
        if percent is not None:
            self._set_calc_progress(percent, label)
        else:
            self.search_state.set(label)
        if not self.search_visual_var.get() or self.canvas is None:return
        from routing import Pt
        def as_pt(p):
            if isinstance(p,Pt):return p
            if isinstance(p,(tuple,list)) and len(p)>=2:return Pt(float(p[0]),float(p[1]))
            if hasattr(p,'u') and hasattr(p,'v'):return Pt(p.u,p.v)
            raise TypeError(f'ожидалась точка U/V, получено {type(p)!r}')
        try:
            c=self.canvas
            c.delete('search_overlay')
            if info.get('point'):
                pt=as_pt(info['point']);x,y=self.to_px(pt)
                c.create_oval(x-3,y-3,x+3,y+3,outline='#f18a27',fill='#f18a27',tags='search_overlay')
            path=info.get('path')
            if path and len(path)>1:
                pts=[as_pt(p) for p in path]
                for a,b in zip(pts,pts[1:]):
                    self._line(a,b,fill='#f18a27',width=3,dash=(5,3),tags='search_overlay')
            segs=info.get('segments')
            if segs:
                for segment in segs:
                    if segment.purpose in ('Хребет','Перспективный хребет',
                                          'Перспективное продление хребта','Центральная плита: магистральная труба'):
                        continue
                    self._line(segment.a,segment.b,fill='#f18a27',width=2,dash=(4,3),tags='search_overlay')
        except Exception:
            # Оверлей поиска не должен ронять «Рассчитать»
            pass

    def _poll_build_results(self):
        # UI event queue is consumed only on the Tk thread.
        while True:
            try:seq,path,result,error=self._build_queue.get_nowait()
            except queue.Empty:break
            if error=='__progress__':
                if seq==self._build_seq and path==self.current_path:
                    self._paint_search_progress(result)
                continue
            self._build_busy=False
            if seq==self._build_seq and path==self.current_path:
                if error is None or error=='__initial_done__':
                    try:
                        if error=='__initial_done__':
                            rendered,catalog,stats=result
                            self._independent_catalog=catalog
                            self._base_result=deepcopy(rendered)
                            result=rendered
                        self._commit_build(result)
                        self.canvas.delete('search_overlay')
                        self._set_calc_progress(100, 'Готово')
                        self.compute_status.set('')
                    except Exception as exc:
                        self._set_calc_progress(0, 'Ошибка обновления плана')
                        messagebox.showerror('Ошибка отображения',str(exc),parent=self)
                else:
                    self._set_calc_progress(0, 'Ошибка расчёта')
                    self.search_state.set('Ошибка: '+str(error)[:140])
                    messagebox.showerror('Ошибка построения',str(error),parent=self)
            if self._build_pending is not None:
                job=self._build_pending
                self._build_pending=None
                self._start_build_worker(job)
        if self._build_busy:self.after(80,self._poll_build_results)
        else:self._build_polling=False

    def _reset_to_elements(self):
        """Исходный чертёж: элементы, НДС, точки и базовый хребет.

        ВАЖНО: здесь нет вызова тяжёлой функции build().
        """
        self.read_form()
        self._build_seq+=1   # игнорировать старые асинхронные результаты
        self._build_pending=None
        self.survey=make_survey(self.project)
        self.result=preview_result(self.survey)
        self.workflow_stage='elements'
        self._slab_snapshot=None
        self.selected_link_element=None
        self.link_candidates=[]
        self.selected_link_index=None
        self.highlighted_trunk_index=None
        self._last_line_pick=None
        self.link_catalog={}
        self._independent_catalog={}
        self._base_result=None
        self._base_project_snapshot=None
        self._linked_project_snapshot=None
        self.inspected_segment=None
        self.inspection_points=None
        self.compute_status.set('Исходные элементы · расчёт не запущен')
        self.pick_hint.set('Нажмите «Рассчитать», затем «Оптимизировать». Маршруты ищутся пошагово.')
        self.search_state.set('Маршруты не рассчитаны')
        self.fit()
        self._update_summary()

    def show_links(self):
        """Независимые короткие ортогональные маршруты для выбранного элемента.

        Геометрически допустимые варианты не следует путать с полным набором
        вариантов будущей сети. Пучки на этом этапе не строим.
        """
        try:
            self.read_form()
            self._build_seq+=1
            self._build_pending=None
            self.survey=make_survey(self.project)
            self.result=preview_result(self.survey)
            self.workflow_stage='links'
            self.selected_link_index=None
            self.highlighted_trunk_index=None
            self._last_line_pick=None
            self._slab_snapshot=None
            # Вычислять варианты ПО ТРЕБОВАНИЮ, только у выделенного
            # элемента. Шаг 0,1 м у врезки создаёт тысячи альтернатив;
            # предварительный перебор каждого узла блокировал Tkinter.
            # Словарь остаётся кэшем этапа «Связь».
            self.link_catalog=({e.key:generate_links(self.survey,e.key)
                                for e in self.survey.elements if e.status!='inactive'}
                               if len(self.survey.elements)<=12 else {})
            self._linked_project_snapshot=asdict(self.project)
            if self.selected_link_element not in {e.key for e in self.survey.elements}:
                self.selected_link_element=next((e.key for e in self.survey.elements
                                                  if e.status!='inactive' and e.kind=='well'),None)
            self._refresh_link_candidates()
            self.compute_status.set('Связь: только осевые отпайки и пожарный узел (внешние трассы отключены)')
            self.pick_hint.set('Только собственные маршруты элемента, без объединений. Щелчок по линии — подсветка.')
            self.fit()
            self._update_summary()
        except Exception as exc:
            messagebox.showerror('Связь',str(exc),parent=self)

    def _refresh_link_candidates(self):
        if self.survey is None or self.selected_link_element is None:
            self.link_candidates=[]
        else:
            self.link_candidates=self.link_catalog.get(self.selected_link_element)
            if self.link_candidates is None:
                self.link_candidates=generate_links(self.survey,self.selected_link_element)
                self.link_catalog[self.selected_link_element]=self.link_candidates

    def select_element_at(self,x,y):
        """Выделить физический элемент щелчком, без запуска расчёта сети."""
        if self.survey is None:return None
        selected=None
        best=24.
        for el in self.survey.elements:
            sx,sy=self.to_px(el.center)
            d=math.hypot(sx-x,sy-y)
            threshold=(max(22.,min(60.,2.4*self.zoom)) if el.kind=='well' else 20.)
            if d < best or (selected is None and d < threshold):
                best=d
                selected=el
        if selected is None:
            return None
        self.selected_link_element=selected.key
        self.selected_link_index=None
        self.highlighted_trunk_index=None
        self._last_line_pick=None
        self._refresh_link_candidates()
        self.draw()
        self._update_summary()
        return selected.key

    @staticmethod
    def _point_segment_distance(x,y,a,b):
        """Кратчайшее расстояние от щелчка до отрезка в экранных пикселях."""
        ax,ay=a;bx,by=b
        vx,vy=bx-ax,by-ay
        length2=vx*vx+vy*vy
        if length2<1e-9:return math.hypot(x-ax,y-ay)
        t=max(0.,min(1.,((x-ax)*vx+(y-ay)*vy)/length2))
        return math.hypot(x-(ax+t*vx),y-(ay+t*vy))

    def _path_distance(self,x,y,points):
        if len(points)<2:return float('inf')
        return min(self._point_segment_distance(x,y,self.to_px(a),self.to_px(b))
                   for a,b in zip(points,points[1:]))

    def select_link_at(self,x,y):
        """Выбрать ПОЛНУЮ независимую линию, а не отдельный отрезок.

        При повторном щелчке по наложенным трассам переключается следующий
        маршрут. Базовый хребет также доступен для выбора.
        """
        if self.workflow_stage!='links' or self.result is None:return False
        medium_filter=next((m for m in MEDIA if MEDIA_RU[m]==self.link_filter_var.get()),None)
        hits=[]
        for i,link in enumerate(self.link_candidates):
            if medium_filter is not None and link.medium!=medium_filter:continue
            if not link.path:continue
            d=self._path_distance(x,y,link.path)
            if d<=9.:
                hits.append((round(d,3),'link',i))
        for i,seg in enumerate(self.result.segments):
            if medium_filter is not None and seg.medium!=medium_filter:continue
            d=self._path_distance(x,y,(seg.a,seg.b))
            if d<=9.:
                hits.append((round(d,3),'trunk',i))
        if not hits:return False
        hits.sort(key=lambda item:(item[0],item[1],item[2]))
        best=hits[0][0]
        # Смена близких/наложенных маршрутов: повторный щелчок переключает.
        choices=[(kind,i) for d,kind,i in hits if d<=best+4.]
        anchor=(x,y)
        previous=self._last_line_pick
        if previous and math.hypot(previous[0]-x,previous[1]-y)<12 and previous[2]==tuple(choices):
            index=(previous[3]+1)%len(choices)
        else:
            index=0
        kind,i=choices[index]
        self._last_line_pick=(x,y,tuple(choices),index)
        self.selected_link_index=i if kind=='link' else None
        self.highlighted_trunk_index=i if kind=='trunk' else None
        if kind=='link':
            chosen=self.link_candidates[i]
            target=chosen.target.split(':',1)[0]
            shown_target=chosen.target
            self.pick_hint.set(f'ВЫДЕЛЕНО {index+1}/{len(choices)}: {MEDIA_RU[chosen.medium]}, '
                               f'{chosen.source} → {shown_target} · {chosen.length:.1f} м · '
                               f'{chosen.elbows} поворотов. Повторный щелчок — следующая линия.')
        else:
            seg=self.result.segments[i]
            self.pick_hint.set(f'ВЫДЕЛЕНО {index+1}/{len(choices)}: базовый хребет · '
                               f'{MEDIA_RU[seg.medium]}, {seg.length:.1f} м. '
                               'Положение сохраняется, но лишний концевой участок может быть удалён.')
        self.draw()
        self._update_summary()
        return True

    def run_calculation(self):
        """Independent small-step path search; all slabs are priced and shown."""
        try:
            if self._build_busy:
                messagebox.showinfo('Расчёт','Предыдущий поиск ещё выполняется.',parent=self)
                return
            self.read_form()
            self.survey=make_survey(self.project)
            self.result=preview_result(self.survey)
            self._base_result=None
            self._independent_catalog={}
            self.workflow_stage='calculating'
            self._slab_snapshot=None
            self._build_seq+=1
            self.draw()
            self.compute_status.set('Ядро + независимые внешние подключения…')
            self.search_state.set('Рассчитать: ядро, затем внешние по отдельности')
            project_snapshot=deepcopy(self.project)
            job=(self._build_seq,project_snapshot,self.current_path,'calculate',
                 deepcopy(self.survey),None,None,self.current_name or 'calc')
            self._start_build_worker(job)
        except Exception as exc:
            messagebox.showerror('Рассчитать',str(exc),parent=self)

    def run_optimization(self):
        """Only here may a new tee or common corridor be proposed."""
        try:
            if self._build_busy:
                messagebox.showinfo('Оптимизация','Предыдущий поиск ещё выполняется.',parent=self)
                return
            if self._base_result is None or not self._independent_catalog:
                messagebox.showinfo('Оптимизация','Сначала нажмите «Рассчитать».',parent=self)
                return
            self.read_form()
            if asdict(self.project)!=self._base_project_snapshot:
                messagebox.showinfo('Изменённый проект',
                    'Исходные данные изменились после расчёта. Сначала нажмите «Рассчитать».',parent=self)
                return
            self._build_seq+=1
            self.workflow_stage='calculating'
            self.compute_status.set('Оптимизация: комбинации внешних путей…')
            self.search_state.set('Общая труба одной среды и общие плиты — один раз')
            self.draw()
            job=(self._build_seq,deepcopy(self.project),self.current_path,
                 'optimize',deepcopy(self.survey),deepcopy(self._base_result),
                 deepcopy(self._independent_catalog),self.current_name or 'opt')
            self._start_build_worker(job)
        except Exception as exc:
            messagebox.showerror('Оптимизировать',str(exc),parent=self)

    def run_plates(self):
        """Reveal fixed-first supports already priced during «Рассчитать».

        Routing and support geometry are immutable at this stage.  Mandatory
        slabs were placed first, filler slabs second, BEFORE the configuration
        was accepted.  We must not silently add slabs after scoring.
        """
        if self.workflow_stage=='calculating':
            messagebox.showinfo('Плиты','Сначала должен завершиться расчёт линий.',parent=self)
            return
        if self.workflow_stage not in ('lines','plates') or self.result is None or self._slab_snapshot is None:
            messagebox.showinfo('Плиты','Сначала нажмите «Рассчитать».',parent=self)
            return
        if self.workflow_stage=='plates':
            self._update_summary();self.draw();return
        from fitting_support import support_sites,unsupported_sites
        slabs=list(self._slab_snapshot)
        missing=unsupported_sites(support_sites(self.result.segments),slabs)
        if missing:
            points='; '.join(f'({q.point.u:.1f}; {q.point.v:.1f})' for q in missing[:4])
            messagebox.showerror('Плиты',f'Не обеспечены обязательные опоры: {points}',parent=self)
            return
        geometry_before=tuple(self.result.segments)
        self.result.slabs=slabs
        if tuple(self.result.segments)!=geometry_before:
            raise ValueError('Этап «Плиты» не имеет права изменять линии из «Связь».')
        self.workflow_stage='plates'
        self.compute_status.set(f'Плиты: фиксированные установлены первыми, заполнителей {int(self.result.metrics.get("filler_plate_count",0))} шт.')
        self._update_summary()
        self.draw()

    def rebuild(self,silent=False):
        """Изменение исходных данных обновляет только неизменяемый базовый хребет."""
        if self._loading_form:return
        try:
            self._reset_to_elements()
        except Exception as exc:
            if not silent:messagebox.showerror('Исходные элементы',str(exc),parent=self)

    def _update_summary(self):
        r=self.result
        if r is None:return
        if self.workflow_stage in ('elements','links'):
            count=len(self.survey.wells) if self.survey else 0
            self.header.set('ЭЛЕМЕНТЫ · БАЗОВЫЙ ХРЕБЕТ · ОСЬ НДС · ВРЕЗКА · ПОЖАРНЫЙ УЗЕЛ')
            self.warnings.configure(state='normal')
            self.warnings.delete('1.0','end')
            self.warnings.insert('1.0','Предварительный хребет: независимые параллельные нитки; '
                'после выбора подключений незадействованные концы удаляются. В «Связь» показываются '
                'два ПРЕДВАРИТЕЛЬНЫХ положения центральной плиты: слева и справа. '
                'Словарь: Элемент — узел входа/выхода. '
                'Линия — трубопровод между входом и выходом. '
                'Связь — только независимый вариант подключения выбранного элемента. '
                'Соединения соседних скважин и общие нитки проверяются только в «Рассчитать». '
                'Точки базового хребта физически определены; порты плит предварительные.')
            self.warnings.configure(state='disabled')
            if self.workflow_stage=='elements':
                side='сверху (+V)' if self.survey.trunk_side==1 else 'снизу (−V)'
                plus,minus=self.survey.side_votes
                self.summary.set(f'Элементов: {count} скважин, точка врезки, пожарный узел. '
                                 f'Базовый хребет {side}: {len(self.result.segments)} ниток, '
                                 f'{sum(s.length for s in self.result.segments):.1f} м. '
                                 f'Элементов выше/ниже НДС: {plus}/{minus}. '
                                 'Сторона хребта зафиксирована; конечная длина уточняется после выбора подключений. '
                                 'Отпайки и плиты не рассчитаны; шаг 1 — «Связь».')
            else:
                stat=link_stats(self.link_candidates)
                label=next((e.label for e in self.survey.elements if e.key==self.selected_link_element),'не выбран')
                axial_selected=next((wp for wp in self.survey.wells
                                     if f'well:{wp.well.number}'==self.selected_link_element
                                     and wp.well.on_axis),None)
                if axial_selected is not None:
                    self.summary.set(f'Связь — {label}: '
                                     f'{stat["total"]} обязательных подключения(ий), '
                                     'по одной ближайшей точке хребта на каждую среду. '
                                     f'Допустимых: {stat["trunk"]}; недопустимых: {stat["rejected"]}. '
                                     'Другие осевые подключения и альтернативные стороны исключены.')
                    return
                self.summary.set(f'Связь — {label}. Вариантов {stat["total"]}: '
                                 f'кратчайших к физическим элементам {stat["shortest"]}, '
                                 f'соединений с базовым хребтом {stat["trunk"]}, '
                                 f'предварительных портов плит {stat["hypothetical"]}, '
                                 f'без допустимого маршрута {stat["rejected"]}, '
                                 f'безопасных обходов {stat["detours"]}. ')
            return
        lines=[]
        for m in MEDIA:
            x=r.stats[m]
            if x["total"]>1e-6:
                lines.append(f"{MEDIA_RU[m]}: {x['active']:.1f} м сейчас + {x['future']:.1f} м перспектива")
        total=sum(z["total"] for z in r.stats.values())
        corridor=r.metrics.get('shared_corridor_m',0.)
        compare=''
        if r.project.shared_corridors and 'original_corridor_m' in r.metrics:
            dc=r.metrics['original_corridor_m']-corridor
            dp=total-r.metrics['original_pipe_m']
            compare=(f" · Коридоры: {dc:+.1f} м экономия" if dc>0 else f" · Коридоры: {dc:.1f} м изменения")
            compare+=f" · Δ труб: {dp:+.1f} м (относительно раздельной прокладки)"
        opt_j=r.metrics.get('optimized_score',0.)
        initial_j=r.metrics.get('original_score',opt_j)
        self.summary.set("    ·    ".join(lines)+
            f"\nИТОГО: {total:.1f} м труб · центральных плит: 1 · плит осевых отпайков 3×1,75 м: {int(r.metrics.get('axial_plate_count',0))} · пучки: {'включены' if r.project.shared_corridors else 'выключены'} · групп: {len(r.routing_groups)}{compare}"
            f"\nРасчёт J: {initial_j:,.0f} → {opt_j:,.0f} ₽; отводов {int(r.metrics.get('topological_elbows',0))}, тройников {int(r.metrics.get('topological_tees',0))}, пересечений {len(r.crossings)} (2D, без стоимостного штрафа). Защитные зоны 5 м."
            f"\nСОКРАЩЕНИЕ ХРЕБТА: удалено незадействованных хвостов {r.metrics.get('trimmed_trunk_m',0):.2f} м по сумме ниток."
            f"\nМЕЖОСЕВОЕ РАССТОЯНИЕ ТРАНЗИТОВ: не менее 0,30 м; нарушений "
            f"{int(r.metrics.get('transit_spacing_issues_after',0))}, малых замен из «Связь» "
            f"{int(r.metrics.get('transit_spacing_catalogue_swaps',0))}; "
            f"плит с недостаточным межтрубным зазором: {int(r.metrics.get('bundle_plate_gap_issues',0))}."
            f"\nОЧЕРЁДНОСТЬ ВНЕШНИХ ТРУБ: неустранённых пересечений "
            f"{int(r.metrics.get('external_transit_crossings',0))}; возможна лишь перестановка уже известных связей."
            f"\nКРАЙНИЕ ФИКСИРОВАННЫЕ ПЛИТЫ У ЗОН 1,5 м: {int(r.metrics.get('boundary_fixed_plate_count',0))} шт."
            f"\nПЛИТЫ 1,2×3 м (тарифируемые): {int(r.metrics.get('small_plate_count',0))} × {r.project.plate_cost:,.0f} ₽/шт. "
            f"· труба {r.project.pipe_cost_per_m:,.0f} ₽/м · отвод {r.project.elbow_cost:,.0f} ₽/шт. · тройник {r.project.tee_cost:,.0f} ₽/шт."
            f"\nПЛИТЫ: центральная 1 · осевые подключения {int(r.metrics.get('axial_plate_count',0))} · отпайки {int(r.metrics.get('axial_branch_plate_count',0))} · промежуточные хребтовые {int(r.metrics.get('intermediate_trunk_plate_count',0))} · локальные {int(r.metrics.get('endpoint_plate_count',0))} · регулярные транзитные {int(r.metrics.get('transit_run_plate_count',0))} · опоры узлов {int(r.metrics.get('fitting_support_plate_count',0))} · хребтовые плиты внешних тройников {int(r.metrics.get('trunk_tap_plate_count',0))}."
            f"\nОПОРЫ УЗЛОВ: проверено {int(r.metrics.get('fitting_site_count',0))} пересечений / тройников / отводов; без опоры (>0,25 м): {int(r.metrics.get('fitting_unsupported_count',0))}.")
        audit=r.decision_trace.get('_integrity_audit')
        if audit and not audit.get('partial_nonfire') and not audit.get('failed'):
            self.summary.set(self.summary.get()+
                f'\nСВЯЗНОСТЬ МАГИСТРАЛЕЙ: проверены {audit.get("checked",0)} из {audit.get("checked",0)} обязательных коммуникаций; обрывов нет. '
                'Подробный протокол — «Детали» → «Фактические подключения».')
        elif self.workflow_stage in ('lines','plates'):
            self.summary.set(self.summary.get()+
                '\n⚠ СВЯЗНОСТЬ: нет полного протокола, необходим повторный расчёт в v93c.')
        comparison=r.decision_trace.get('_global_comparison',{})
        if comparison and r.project.shared_corridors:
            base=comparison.get('direct_J')
            alt=comparison.get('optimized_J')
            if base is not None and alt is not None:
                bundles=comparison.get('groups',[])
                self.summary.set(self.summary.get()+
                    f'\nПУЧКИ: независимая конфигурация {base:,.0f} ₽ · '
                    f'лучший проверенный вариант {alt:,.0f} ₽ · '
                    f'группы {bundles if bundles else "не выбраны"}; '
                    f'плит 1,2×3 м: {comparison.get("direct_small_plates",0)} → '
                    f'{comparison.get("optimized_small_plates",0)}.')
        if r.decision_trace.get('_catalog_only') and 'catalogue_refinement_networks' in r.metrics:
            saving=r.metrics.get('catalogue_refinement_saving',0.0)
            attempts=int(r.metrics.get('catalogue_refinement_networks',0))
            runs=r.decision_trace.get('_potential_shared_support_runs',[])
            self.summary.set(self.summary.get()+
                f'\nПОИСК ПУЧКОВ: дополнительно проверено {attempts} допустимых вариантов из «Связь»; '
                f'экономия на выбранном улучшении {saving:,.0f} ₽; '
                f'геометрических сближений под общую 1,2-метровую плиту: {len(runs)}. '
                'Пожарная линия не участвует, трубы не переносятся.')
        shared=r.decision_trace.get('_existing_shared_lines',[])
        if shared:
            self.summary.set(self.summary.get()+
                f'\nСОВПАДАЮЩИЕ ТРУБОПРОВОДЫ: {len(shared)} участков независимых связей; '
                f'общая длина совпадений {sum(x["shared_m"] for x in shared):.1f} м. '
                'Вычислено только после «Рассчитать», без новой трассировки.')
        if False and r.decision_trace.get('_catalog_only'):
            n=int(r.metrics.get('catalogue_selected_links',0))
            checked=int(r.metrics.get('catalogue_evaluated_networks',0))
            self.summary.set(self.summary.get()+
                f'\nКАТАЛОГ «СВЯЗЬ»: {n} подключений, каждое совпадает с существующим вариантом. '
                f'Оценено совместимых конфигураций: {checked}. '
                'Общий коридор не меняет геометрию маршрутов. Поиск приближённый.')
        if self.workflow_stage=='lines':
            self.summary.set(self.summary.get()+'\nЭтап «Рассчитать»: показаны линии; плиты появятся после отдельной кнопки «Плиты». '
                             'Глобальный минимум J ещё не доказан: оптимизатор проверяет ограниченный набор комбинаций. '
                             'Дальнейшие тройники и пучки создаются только кнопкой «Оптимизировать».')
        step_data=r.decision_trace.get('_stepwise_search',{})
        if step_data:
            self.summary.set(self.summary.get()+
                f'\nПОШАГОВЫЙ ПОИСК: шаг сетки {step_data.get("grid_step_m",1.0):g} м · '
                f'допустимых точек порта {step_data.get("targets_checked",0)} · '
                f'повторных A* попыток {step_data.get("paths_examined",0)} · '
                f'узлов графа {step_data.get("grid_nodes_expanded",0)} · '
                f'ограниченных поисков {step_data.get("grid_limit_hits",0)}. '
                'Математический глобальный оптимум не подтверждён.')
        optim_data=r.decision_trace.get('_stepwise_optimization',{})
        if optim_data:
            start_defects=optim_data.get('spacing_defects_before',0)
            end_defects=optim_data.get('spacing_defects_after',0)
            self.summary.set(self.summary.get()+
                f'\nОПТИМИЗАЦИЯ: проверено {optim_data.get("attempted",0)} новых конфигураций, '
                f'совместных пучков {optim_data.get("corridors_tested",0)}, '
                f'из них с тремя средами {optim_data.get("triple_corridors_tested",0)}, '
                f'улучшений {optim_data.get("accepted",0)} · '
                f'стоимость {optim_data.get("independent_cost",0):,.0f} → '
                f'{optim_data.get("best_cost",0):,.0f} ₽. '
                f'Нарушений межосевого расстояния 0,30 м: {start_defects} → {end_defects}. '
                'Если первоначальный вариант нарушал зазоры, сначала устраняются нарушения, '
                'и только среди одинаково допустимых вариантов минимизируется стоимость. '
                'Ограниченный поиск — оптимальность не доказана.')
        if r.decision_trace.get('_well_orientation'):
            orient_text=', '.join(f'№{number}: '+('вдоль НДС' if ori=='parallel' else 'поперёк НДС')
                for number,ori in sorted(r.decision_trace['_well_orientation'].items()))
            self.summary.set(self.summary.get()+f'\nРАЗВОРОТ ВНЕОСЕВЫХ СКВАЖИН ПО ВЫБРАННЫМ СВЯЗЯМ: {orient_text}.')
        if r.project.auto_rotate and r.metrics.get('orientation_candidates_evaluated',0)>1:
            chosen_placements=[wp for wp in r.wells if not wp.well.on_axis]
            orient_text=', '.join(f'№{wp.well.number}: '+('вдоль НДС' if wp.orientation=='parallel' else 'поперёк НДС') for wp in chosen_placements)
            self.summary.set(self.summary.get()+f'\nОРИЕНТАЦИИ (поворот = 0 усл. ед.): {orient_text}. Проверено: {int(r.metrics["orientation_candidates_evaluated"])} вариантов.')
        if r.crossing_markup is not None:
            a=r.crossing_markup
            lengths=[math.hypot(m.end.u-m.start.u,m.end.v-m.start.v) for m in a.markers]
            max_length=max(lengths,default=0.)
            self.summary.set(self.summary.get()+
                f'\nПЕРЕСЕЧЕНИЯ (2D): {len(a.crossings)} точек · {len(a.markers)} условных участков · '
                f'объединённых {a.grouped_count} · без границ {len(a.unassigned)} · '
                f'самый длинный {max_length:.2f} м (в плане). '
                'Физические подъёмы и дополнительные отводы не рассчитаны.')
        count_axial=int(r.metrics.get('axial_branch_crossings',0))
        if count_axial:
            self.summary.set(self.summary.get()+
                f'\n⚠ В ЗОНЕ ОСЕВЫХ ОТПАЙКОВ: {count_axial} пересечений с другими средами; требуется конструктивное решение.')
        count_bad=int(r.metrics.get('foreign_contacts',0))
        if count_bad:
            self.summary.set(self.summary.get()+
                f'\n⚠ ОШИБКА ГЕОМЕТРИИ: {count_bad} касаний труб разных сред. '
                'Это не тройники. Смотрите диагностику перед использованием схемы.')
        center_of_plate=(Pt(r.central_plate.center_u,r.central_plate.center_v)
                         if r.central_plate is not None else r.center)
        wx,wy=r.frame.xy(center_of_plate)
        selected=getattr(r,'selected_center_position','left_upper')
        labels={'left_upper':'СЛЕВА-СВЕРХУ','left_lower':'СЛЕВА-СНИЗУ',
                'right_upper':'СПРАВА-СВЕРХУ','right_lower':'СПРАВА-СНИЗУ'}
        self.header.set((f"⚠ {count_bad} ОШИБОЧНЫХ КАСАНИЙ | " if count_bad else "")+
                        f"2D-ГЕНПЛАН | ПЛИТА {labels.get(selected,'СЛЕВА')} · ЦЕНТР X={wx:.2f}, Y={wy:.2f}")
        notes=r.notes+(r.crossings[:18] if r.crossings else ["Автоматическая проверка внутренних пересечений разных сред не выявила."])
        if not r.project.tie_confirmed:
            notes.insert(0,"ВАЖНО: точка врезки ещё НЕ ЗАДАНА — показано временное положение. Укажите реальные координаты X/Y.")
        if not r.project.fire_confirmed:
            notes.insert(0,"ВАЖНО: пожарный узел ещё НЕ ЗАДАН — показано временное положение. Укажите реальные координаты X/Y.")
        notes.append("Предварительная ведомость материалов: трубы 09Г2С из Excel; опоры и арматура не включены; диаметры требуют проверки.")
        if len(r.crossings)>18:
            notes.append(f"...и ещё {len(r.crossings)-18} пересечений (учтены в отчёте CSV).")
        self.warnings.config(state="normal")
        self.warnings.delete("1.0","end")
        self.warnings.insert("1.0","\n".join("• "+note for note in notes))
        self.warnings.config(state="disabled")

    def view(self)->MapView:
        assert self.result is not None
        return MapView(self.result.frame,self.result.project.coordinate_mode,
                       self.cx,self.cy,self.zoom,
                       max(self.canvas.winfo_width(),1),max(self.canvas.winfo_height(),1))

    def fit(self):
        if self.result is None:
            return
        pts=[w.point for w in self.result.wells]+[self.result.center,self.result.tie,self.result.fire]
        if self.workflow_stage=='links' and self.survey is not None:
            for _,plate in self.survey.plate_variants:
                pts.extend(Pt(u,v) for u,v in plate_draw_data(plate))
        for seg in self.result.segments:
            pts.extend((seg.a,seg.b))
        view=self.view()
        view.width=max(view.width,700);view.height=max(view.height,450)
        view.fit(pts)
        self.cx,self.cy,self.zoom=view.cx,view.cy,view.zoom
        self.draw()

    def to_px(self,p:Pt):
        return self.view().to_screen(p)

    def to_uv(self,x,y):
        return self.view().from_screen(x,y)

    def _line(self,a,b,**kw):
        ax,ay=self.to_px(a);bx,by=self.to_px(b)
        return self.canvas.create_line(ax,ay,bx,by,**kw)

    def draw(self):
        """План с фиксированными сторонами света: север вверх, восток вправо."""
        if self.result is None:return
        c=self.canvas;c.delete("all")
        w=max(c.winfo_width(),1);h=max(c.winfo_height(),1)
        step=5
        while step*self.zoom<55:
            step*=2
        left=self.cx-w/(2*self.zoom);right=self.cx+w/(2*self.zoom)
        bottom=self.cy-h/(2*self.zoom);top=self.cy+h/(2*self.zoom)
        for east in range(math.floor(left/step)*step,math.ceil(right/step)*step+step,step):
            x=w/2+(east-self.cx)*self.zoom
            c.create_line(x,0,x,h,fill=PALETTE["grid"],width=1)
        for north in range(math.floor(bottom/step)*step,math.ceil(top/step)*step+step,step):
            y=h/2-(north-self.cy)*self.zoom
            c.create_line(0,y,w,y,fill=PALETTE["grid"],width=1)
        # Ось НДС — в настоящем дирекционном угле, а не горизонтально.
        u_values=[w.point.u for w in self.result.wells]
        u_max=max([0,self.result.trunk_end_u]+u_values)+18
        self._line(Pt(0,0),Pt(u_max,0),fill="#db5558",dash=(12,6,2,6),width=2)
        xc,yc=self.to_px(Pt(u_max,0))
        c.create_text(xc+8,yc-8,text="ОСЬ НДС",anchor="sw",fill="#b82e38",font=("Arial",11,"bold"))
        # Зоны одинакового радиуса 5 м в локальной метрической системе U/V.
        # Это только слой отображения: переключение НЕ запускает расчёт.
        if self.show_well_guards.get():
            from routing import WELL_GUARD_RADIUS
            radius=WELL_GUARD_RADIUS*self.zoom
            for wp in self.result.wells:
                gx,gy=self.to_px(wp.point)
                c.create_oval(gx-radius,gy-radius,gx+radius,gy+radius,
                              outline='#c78c37',dash=(5,4),width=1.6,
                              fill='#f7e4bb',stipple='gray12',
                              tags=('well_guard',))
                if self.zoom>=2.7:
                    c.create_text(gx+radius+3,gy,text='5 м',anchor='w',
                                  fill='#9e712e',font=('Segoe UI',8),tags=('well_guard',))
        # 1.5 m structural exclusion strip. Geometry is the same as in the
        # catalogue and cost solver; this is only a non-interactive layer.
        if self.show_main_guards.get() and self.survey is not None:
            from structural_clearance import infrastructure_buffers
            if self.workflow_stage in ('lines','plates') and self.result.central_plate is not None:
                from dataclasses import replace as dc_replace
                from structural_clearance import TRUNK
                actual=dc_replace(self.survey,trunk_segments=tuple(
                    s for s in self.result.segments if s.purpose in TRUNK))
                zones=(infrastructure_buffers(actual,self.result.central_plate),)
            elif self.workflow_stage=='links':
                zones=tuple(infrastructure_buffers(self.survey,variant)
                            for _,variant in self.survey.plate_variants)
            else:
                zones=(infrastructure_buffers(self.survey,self.survey.plate_variants[0][1])[:1],)
            visited=set()
            for items in zones:
                for area in items:
                    coords=tuple(round(v,5) for v in area.expanded())
                    if (area.kind,coords) in visited:continue
                    visited.add((area.kind,coords))
                    x0,x1,y0,y1=coords
                    corners=(Pt(x0,y0),Pt(x1,y0),Pt(x1,y1),Pt(x0,y1))
                    points=[v for point in corners for v in self.to_px(point)]
                    c.create_polygon(*points,fill='#d9e4ed',stipple='gray12',
                              outline='#708aa4',dash=(6,4),width=1.4,
                              tags=('structural_guard',))
        # Площадной центральный модуль: рисунок ПОД трубами, поэтому ни одна
        # из пяти магистралей не теряет контур из-за заливки плиты.
        plate=self.result.central_plate
        if self.workflow_stage=='links' and self.survey is not None:
            # Оба места размещения центральной плиты — только предварительные.
            # Ни одна плита не является установленной до кнопки «Плиты».
            for name,variant in self.survey.plate_variants:
                coords=[coordinate for u,v in plate_draw_data(variant)
                        for coordinate in self.to_px(Pt(u,v))]
                c.create_polygon(*coords,fill='#eef4fa',outline='#5c7390',
                                 dash=(7,4),width=2)
                x,y=self.to_px(Pt(variant.center_u,variant.center_v))
                caption='ЦЕНТРАЛЬНАЯ ПЛИТА · СЛЕВА (ВАРИАНТ)' if name=='left' else 'ЦЕНТРАЛЬНАЯ ПЛИТА · СПРАВА (ВАРИАНТ)'
                c.create_text(x,y-17,text=caption,fill='#445b76',font=('Segoe UI',9,'bold'),anchor='s')
        if plate is not None and self.workflow_stage=='plates':
            pts=[]
            for u,v in plate_draw_data(plate):
                pts.extend(self.to_px(Pt(u,v)))
            c.create_polygon(*pts,fill="#d9e3ec",outline="#4f6f8a",width=2)
        # Все вспомогательные плиты также рисуются ПОД трубами.
        support_plates=[s for s in self.result.slabs if s.slab_type!='central']
        for slab in support_plates:
            corners=((slab.u_start,slab.v_min),(slab.u_end,slab.v_min),
                     (slab.u_end,slab.v_max),(slab.u_start,slab.v_max))
            pts=[coordinate for u,v in corners for coordinate in self.to_px(Pt(u,v))]
            filler=slab.slab_type in ('transit_run','fire_transit_run')
            c.create_polygon(*pts,fill="#d9e3ec" if filler else "#efe1c7",
                             outline="#4f6f8a" if filler else "#a67b3e",width=2)
        # Защитные зоны 5 м учитываются расчётным ядром, но на плане скрыты.
        # Оба режима используют одну и ту же рассчитанную сеть и не меняют метраж.
        if self.pipe_mode.get():
            for primitive in primitives(self.result.segments,lambda q:self.to_px(Pt(*q)),
                                        self.zoom,background=BACKGROUND):
                if primitive.kind=='line':
                    (x1,y1),(x2,y2)=primitive.points
                    kw={"fill":primitive.stroke,"width":primitive.width,"capstyle":"round"}
                    if primitive.dash:
                        kw['dash']=tuple(int(v) for v in primitive.dash.split())
                    c.create_line(x1,y1,x2,y2,**kw)
                elif primitive.kind=='polygon':
                    flat=[v for point in primitive.points for v in point]
                    kw={'outline':primitive.stroke,'fill':primitive.fill or '', 'width':primitive.width}
                    if primitive.dash:kw['dash']=tuple(int(v) for v in primitive.dash.split())
                    c.create_polygon(*flat,**kw)
                elif primitive.kind=='ellipse':
                    (x,y),(rx,ry)=primitive.points
                    kw={'outline':primitive.stroke,'fill':primitive.fill or '', 'width':primitive.width}
                    if primitive.dash:kw['dash']=tuple(int(v) for v in primitive.dash.split())
                    c.create_oval(x-rx,y-ry,x+rx,y+ry,**kw)
        else:
            for s in sorted(self.result.segments,key=lambda a:a.phase=="future"):
                args={"fill":MEDIA_COLOR[s.medium],"width":3 if s.phase=="active" else 2,"capstyle":"projecting"}
                if s.phase=="future":args["dash"]=(8,5)
                self._line(s.a,s.b,**args)
        # Чисто графическое перекрытие: выбранная труба визуально закрывает нижнюю.
        if self.vertical_enabled.get() and self.result.crossing_markup is not None:
            for primitive in visual_cover_primitives(self.result.crossing_markup,
                                                    lambda q:self.to_px(q),self.zoom,
                                                    detailed=self.pipe_mode.get(),background=BACKGROUND,
                                                    central_plate=plate,support_slabs=support_plates):
                if primitive.kind=='line':
                    (x1,y1),(x2,y2)=primitive.points
                    kw={"fill":primitive.stroke,"width":primitive.width,"capstyle":"round"}
                    if primitive.dash:
                        kw['dash']=tuple(int(v) for v in primitive.dash.split())
                    c.create_line(x1,y1,x2,y2,**kw)
                elif primitive.kind=='polygon':
                    flat=[v for point in primitive.points for v in point]
                    c.create_polygon(*flat,outline=primitive.stroke,fill=primitive.fill or '',width=primitive.width)
            for item in overlay_primitives(self.result.crossing_markup,
                                          lambda q:self.to_px(q),self.zoom):
                if item[0]=='line':
                    _,pa,pb,color,width=item
                    c.create_line(*pa,*pb,fill=color,width=width)
                elif item[0]=='text':
                    _,pos,text,color,size=item
                    c.create_text(*pos,text=text,fill=color,font=('Segoe UI',int(size),'bold'),anchor='sw')
        # Выбранная труба подсвечивается поверх 2D-переходов.
        if (self.inspect_mode.get() and self.inspected_segment is not None
            and 0 <= self.inspected_segment < len(self.result.segments)):
            chosen=self.result.segments[self.inspected_segment]
            self._line(chosen.a,chosen.b,fill='#ff8a00',width=5,
                       capstyle='round',dash=(8,4))
        # Все четыре стороны источника и дискретные допустимые/проверенные
        # варианты окончания выбранной линии. Список расширенный, он не
        # приписывает оптимизатору оценку непроверенных точек.
        if self.inspect_mode.get() and self.show_candidate_points.get() and self.inspection_points:
            points=self.inspection_points
            for i,item in enumerate(points['ends']):
                q=item['point']; px,py=self.to_px(q)
                if not (-10 <= px <= w+10 and -10 <= py <= h+10):continue
                checked=item['status']=='локально оценён'
                color='#11885e' if checked else '#8a94a3'
                size=5 if checked else 3
                c.create_oval(px-size,py-size,px+size,py+size,
                             fill=color,outline='white',width=1,tags=('end_candidate',))
            for i,item in enumerate(points['starts']):
                q=item['point'];px,py=self.to_px(q)
                r=7
                col='#a13de0' if item['status']!='выбран' else '#e85b15'
                c.create_polygon(px,py-r,px+r,py,px,py+r,px-r,py,
                                 fill=col,outline='white',width=1,tags=('start_candidate',))
            c.create_rectangle(285,10,688,66,outline='#9ca9b7',fill='#ffffff')
            c.create_text(294,18,text=f"ВЫБОР: {len(points['starts'])} начала · {len(points['ends'])} концов · {points['actual_checked']} оценено",anchor='nw',font=('Segoe UI',9,'bold'),fill='#283e54')
            c.create_text(294,41,text='◆ фиолетовый — выход · ● зелёный — проверено · ● серый — не проверено',anchor='nw',font=('Segoe UI',8),fill='#475a70')
        # Физические торцевые заглушки на неиспользованных портах плиты.
        # Трубы продолжаются через торец без заглушки, если есть внешний сегмент.
        if plate is not None:
            used_plate_media=tuple(sorted({s.medium for s in self.result.segments if s.purpose=='Центральная плита: магистральная труба'}))
            for medium,pos,_face in capped_ports(plate,self.result.segments,self.result.lanes,used_plate_media):
                half=max(PIPE_OD[medium]/2,.07)
                pts=[]
                for u,v in ((pos.u-.12,pos.v-half),(pos.u+.12,pos.v-half),
                            (pos.u+.12,pos.v+half),(pos.u-.12,pos.v+half)):
                    pts.extend(self.to_px(Pt(u,v)))
                c.create_polygon(*pts,fill=MEDIA_COLOR[medium],outline="#273747",width=1.3)
        # Начальный торец хребта сохраняется в расчёте, но не показывается.
        for med,port in self.result.tie_ports.items():
            xx,yy=self.to_px(port)
            c.create_oval(xx-3,yy-3,xx+3,yy+3,fill=MEDIA_COLOR[med],outline="#ffffff")
        if self.workflow_stage=='links' and self.survey is not None:
            medium_filter=next((m for m in MEDIA if MEDIA_RU[m]==self.link_filter_var.get()),None)
            drawn=set()
            for i,candidate in enumerate(self.link_candidates):
                if medium_filter is not None and candidate.medium!=medium_filter:continue
                if not candidate.path:continue
                coords=tuple((round(q.u,4),round(q.v,4)) for q in candidate.path)
                key=(candidate.medium,coords)
                if key in drawn:continue
                drawn.add(key)
                color=MEDIA_COLOR.get(candidate.medium,'#5c7b9c')
                dash=(3,7) if candidate.target_kind=='element' else (2,10)
                for a,b in zip(candidate.path,candidate.path[1:]):
                    self._line(a,b,fill=color,width=1,dash=dash)
            # Подсветка ПОЛНОЙ выбранной линии поверх остальных, с белой
            # подложкой: даже совпадающие трассы и цвета будут различимы.
            selected_path=()
            if self.selected_link_index is not None and self.selected_link_index<len(self.link_candidates):
                link=self.link_candidates[self.selected_link_index]
                if medium_filter is None or link.medium==medium_filter:
                    selected_path=link.path
            elif (self.highlighted_trunk_index is not None and
                  self.highlighted_trunk_index<len(self.result.segments)):
                segment=self.result.segments[self.highlighted_trunk_index]
                if medium_filter is None or segment.medium==medium_filter:
                    selected_path=(segment.a,segment.b)
            for a,b in zip(selected_path,selected_path[1:]):
                self._line(a,b,fill='#ffffff',width=10,capstyle='round')
                self._line(a,b,fill='#ff8600',width=5,capstyle='round')
            if self.selected_link_element:
                el=next((e for e in self.survey.elements if e.key==self.selected_link_element),None)
                if el:
                    sx,sy=self.to_px(el.center)
                    c.create_oval(sx-12,sy-12,sx+12,sy+12,outline='#ff9100',width=3)

        for loc in self.result.wells:
            x,y=self.to_px(loc.point)
            clr={'active':'#de3345','future':'#c19321','inactive':'#919aa6'}[loc.well.status]
            # Прямоугольник рисуется В МАСШТАБЕ локальной геометрии.
            # Так отпаечный трубопровод не выглядит подключённым к торцу
            # из-за расхождения между размером символа и расчётным выходом.
            half_u,half_v=well_dimensions(loc)
            corners=[self.to_px(Pt(loc.point.u+du,loc.point.v+dv)) for du,dv in
                     ((-half_u,-half_v),(half_u,-half_v),(half_u,half_v),(-half_u,half_v))]
            pixel=[]
            for q in corners:pixel.extend(q)
            c.create_polygon(*pixel,outline=clr,width=2.4,fill='white',
                             dash=(6,3) if loc.well.status=='future' else None)
            c.create_line(*corners[0],*corners[2],fill=clr,width=1.3)
            c.create_line(*corners[1],*corners[3],fill=clr,width=1.3)
            # Каждая среда выходит отдельной трубой из своей точки
            # на длинной стороне — без фиктивного общего штуцера.
            if self.workflow_stage in ('lines','plates','calculating'):
                for med,outlet in well_medium_outlets(loc,self.result.trunk_side,
                                                       self.result.center,self.result.project.lane_gap).items():
                    xp,yp=self.to_px(outlet)
                    c.create_oval(xp-3,yp-3,xp+3,yp+3,fill=MEDIA_COLOR[med],outline='white',width=1)
            c.create_text(x,y+38,text=f'{loc.well.number}',  
                          fill='#b320aa',font=('Arial',13,'bold'),anchor='n')
            if loc.well.status=='future':
                c.create_text(x+17,y-13,text='ПЕРСП.',fill='#a47b16',anchor='sw',font=('Arial',8))
        if plate is not None and self.workflow_stage=='plates':
            x,y=self.to_px(Pt(plate.center_u,plate.center_v))
            c.create_text(x,y-12,text="ЦЕНТРАЛЬНАЯ ПЛИТА 6×1,75 м",
                          fill="#1e4f6e",font=("Arial",10,"bold"),anchor="s")
        for point,label,color in ((self.result.tie,"ТОЧКА ВРЕЗКИ","#212529"),
                                  (self.result.fire,"ПОЖАРНЫЙ УЗЕЛ","#c82b30")):
            if point is None:continue
            x,y=self.to_px(point)
            c.create_rectangle(x-5,y-5,x+5,y+5,fill="white",outline=color,width=2)
            c.create_text(x+10,y-9,text=(label+" · ТОЛЬКО НЕФТЬ" if label=="ТОЧКА ВРЕЗКИ" else label),fill=color,font=("Arial",10,"bold"),anchor="sw")
        # Компактная легенда CAD-стиля.
        c.create_rectangle(8,10,267,191,outline="#a8b4bf",fill="#ffffff",width=1)
        c.create_text(20,21,text="УСЛОВНЫЕ ОБОЗНАЧЕНИЯ",fill="#262f36",font=("Arial",10,"bold"),anchor="nw")
        for i,med in enumerate(MEDIA):
            y=46+i*21
            c.create_line(20,y,55,y,fill=MEDIA_COLOR[med],width=3)
            c.create_text(65,y,text=MEDIA_RU[med],fill="#242424",font=("Arial",9),anchor="w")
        legend_text=("Центральная плита: 6 × 1,75 м" if self.workflow_stage=='plates'
                     else "Словарь: элемент — узел; линия — трубопровод")
        c.create_text(20,160,text=legend_text,fill="#687887",anchor="sw",font=("Arial",8))
        c.create_text(20,179,text="Колесо: масштаб; Shift+колесо: прокрутка плана",fill="#687887",anchor="sw",font=("Arial",8))
        x,y=w-52,75
        c.create_line(x,y,x,y-35,arrow="last",fill="#1c242b",width=2)
        c.create_text(x,y-47,text="С",font=("Arial",11,"bold"),fill="#1c242b")
        c.create_text(x,y+18,text=f"НДС {self.result.project.azimuth:g}°",font=("Arial",9),fill="#1c242b")
        sx,sy=18,h-26
        c.create_line(sx,sy,sx+20*self.zoom,sy,fill="#1c242b",width=3)
        c.create_text(sx,sy-12,text="20 м",anchor="w",font=("Arial",9),fill="#1c242b")
        c.create_text(w-9,h-12,text="ВИД СВЕРХУ | ЦЕНТР: "+getattr(self.result,'selected_center_position','left_upper').upper()+" | 2D",
                      fill="#6b7885",anchor="e",font=("Arial",9))

    def export_crossings_csv(self):
        try:
            self.read_form()
            if self.workflow_stage not in ('lines','plates'):
                raise ValueError('Сначала нажмите «Рассчитать».')
            path=filedialog.asksaveasfilename(parent=self,title='Ведомость участков пересечений',
                            defaultextension='.csv',initialfile='Участки_пересечений.csv',
                            filetypes=[('CSV','*.csv')])
            if path:
                export_crossings_csv(self.result,path)
                messagebox.showinfo('Готово','Условные 2D-участки и координаты сохранены.',parent=self)
        except Exception as exc:
            messagebox.showerror('Экспорт участков пересечений',str(exc),parent=self)

    def on_wheel_pan(self,event,explicit=None):
        """Shift+колесо: вертикальное перемещение генплана без зума."""
        if explicit is None:
            d=getattr(event,'delta',0)
            explicit=-1 if d>0 else 1 if d<0 else 0
        if explicit:
            self.cy+=explicit*55/max(self.zoom,.02)
            self.draw()
        return 'break'

    def on_zoom(self,e):
        self._zoom(e,1.10 if e.delta>0 else 1/1.10)

    def _zoom(self,e,factor):
        view=self.view()
        view.zoom_at(e.x,e.y,factor)
        self.cx,self.cy,self.zoom=view.cx,view.cy,view.zoom
        self.draw()

    def _azimuth_changed(self,*args):
        if self._loading_form:return
        if self._auto_job is not None:
            self.after_cancel(self._auto_job)
        self._auto_job=self.after(400,self._autobuild_azimuth)

    def _autobuild_azimuth(self):
        self._auto_job=None
        try:
            angle=num(self.fields['azimuth'].get(),'Азимут НДС')
            if not 0<=angle<=360:return
            self.rebuild(silent=True)
        except (ValueError,KeyError):pass

    def begin_pick(self,label,callback,on_cancel=None):
        if self._auto_job is not None:
            self.after_cancel(self._auto_job)
            self._auto_job=None
        self.rebuild(silent=True)
        if self.result is None:return
        if self.pick_cancel_callback:self.cancel_pick()
        self.pick_callback=callback
        self.pick_cancel_callback=on_cancel
        self.canvas.configure(cursor='crosshair')
        self.pick_hint.set(f"ВЫБОР: {label}. Щёлкните на плане; Esc — отмена.")
        self.canvas.focus_set()

    def cancel_pick(self,event=None):
        if self.pick_callback is None:return
        self.pick_callback=None
        callback=self.pick_cancel_callback
        self.pick_cancel_callback=None
        self.canvas.configure(cursor='')
        self.pick_hint.set("Выбор отменён. Север сверху; азимут поворачивает схему.")
        if callback:callback()

    def pick_coordinate_pair(self,which):
        titles={'first':'первая скважина','tie':'точка врезки','fire':'пожарный узел','center':'центральный узел'}
        def chosen(x,y):
            self.fields[which+'_x'].set(f'{x:.3f}')
            self.fields[which+'_y'].set(f'{y:.3f}')
            if which=='center':
                pass
            elif which=='tie':
                self.project.tie_confirmed=True
            elif which=='fire':
                self.project.fire_confirmed=True
            self.rebuild()
        self.begin_pick(titles[which],chosen)

    def on_canvas_press(self,event):
        if self.workflow_stage=='links' and self.pick_callback is None and not (event.state & 0x1):
            # Клик внутри символа элемента меняет набор связей. Клик по
            # свободному участку линии выбирает именно эту трассу.
            if self.survey is not None and any(
                math.hypot(self.to_px(el.center)[0]-event.x,
                           self.to_px(el.center)[1]-event.y)<
                (max(16.,min(34.,2.2*self.zoom)) if el.kind=='well' else 15.)
                for el in self.survey.elements):
                if self.select_element_at(event.x,event.y) is not None:return 'break'
            if self.select_link_at(event.x,event.y):return 'break'
        if self.inspect_mode.get() and self.pick_callback is None and not (event.state & 0x1):
            self.select_route_at(event.x,event.y)
            return 'break'
        if self.pick_callback is not None:
            callback=self.pick_callback
            self.pick_callback=None
            self.pick_cancel_callback=None
            self.canvas.configure(cursor='')
            x,y=self.view().world_from_screen(event.x,event.y)
            self.pick_hint.set(f"Указаны координаты: X={x:.3f}  Y={y:.3f}")
            callback(x,y)
            return
        self.pan_start(event)

    def on_canvas_move(self,event):
        if self.pick_callback is None or self.result is None:return
        x,y=self.view().world_from_screen(event.x,event.y)
        self.pick_hint.set(f"Щёлкните: X={x:.3f}, Y={y:.3f} · Esc отмена")

    def pan_start(self,e):
        if self.pick_callback:return
        self.drag=(e.x,e.y,self.cx,self.cy)

    def pan(self,e):
        if ((self.inspect_mode.get() or self.workflow_stage=='links') and not (e.state & 0x1)) or self.pick_callback or self.drag is None:return
        x,y,cx,cy=self.drag
        self.cx=cx-(e.x-x)/self.zoom
        self.cy=cy+(e.y-y)/self.zoom
        self.draw()

    def show_home(self):
        self.withdraw()
        if self.home is None or not self.home.winfo_exists():
            self.home=ProjectHome(self,self.project_store)
        else:
            self.home.refresh()
            self.home.deiconify()
            self.home.lift()
        self.home.focus_force()

    def create_managed_project(self,name,project):
        try:
            path=self.project_store.create(name,project)
            self.open_managed_project(path)
        except Exception as exc:
            messagebox.showerror('Создание проекта',str(exc),parent=self.home or self)

    def open_managed_project(self,path):
        self._build_seq+=1
        try:
            loaded=self.project_store.load(path)
            self.current_path=Path(path)
            from json import loads
            self.current_name=str(loads(self.current_path.read_text(encoding='utf-8')).get('_name') or self.current_path.stem)
            self.project=loaded
            # Инженерную графику пользователь включает вручную для каждого проекта.
            self.pipe_mode.set(False)
            self.load_project_into_form()
            if self.home is not None:
                self.home.withdraw()
            self.deiconify()
            # Windows: максимизированное окно с панелью задач. Другие WM
            # могут проигнорировать zoomed без исключения: это не успех.
            try:
                self.state('zoomed')
            except tk.TclError:
                pass
            self.update_idletasks()
            if self.state() != 'zoomed':
                try:
                    self.attributes('-fullscreen', True)
                except tk.TclError:
                    pass
                self.update_idletasks()
                if not self.attributes('-fullscreen'):
                    self.geometry(f'{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0')
            self.title(f'«{self.current_name}» — Хребет v93c (два этапа)')
            self._reset_to_elements()
            self.lift()
            self.focus_force()
        except Exception as exc:
            messagebox.showerror('Открытие проекта',str(exc),parent=self.home or self)

    def back_to_home(self):
        self._build_seq+=1  # не отображать устаревший результат после смены проекта
        if self.current_path is not None:
            try:
                self.read_form()
                self.project_store.save(self.current_path,self.project,self.current_name)
            except Exception as exc:
                messagebox.showerror('Сохранение перед выходом',
                                     f'Невозможно сохранить проект:\n{exc}\n\nИсправьте поля и повторите.',parent=self)
                return
        self.cancel_pick()
        self.current_path=None
        self.current_name=None
        self.show_home()

    def save(self):
        try:
            self.read_form()
            if self.current_path is None:
                raise ValueError('Сначала создайте проект через стартовый экран.')
            self.project_store.save(self.current_path,self.project,self.current_name)
            messagebox.showinfo('Готово',f'Проект «{self.current_name}» сохранён.\nПапка: Проекты',parent=self)
        except Exception as exc:
            messagebox.showerror('Сохранение',str(exc),parent=self)

    def save_copy(self):
        try:
            self.read_form()
            path=filedialog.asksaveasfilename(parent=self,title='Сохранить копию JSON',defaultextension='.json',
                                              filetypes=[('Проект JSON','*.json')])
            if path:
                self.project.save(path)
                messagebox.showinfo('Готово',f'Копия проекта сохранена:\n{path}',parent=self)
        except Exception as exc:
            messagebox.showerror('Сохранить копию',str(exc),parent=self)

    def open(self):
        path=filedialog.askopenfilename(parent=self,title='Импортировать старый проект',filetypes=[('Проект JSON','*.json')])
        if not path:
            return
        try:
            project=Project.load(path)
            self.read_form()
            if self.current_path is not None:
                self.project_store.save(self.current_path,self.project,self.current_name)
            import_path=self.project_store.create(Path(path).stem,project)
            self.open_managed_project(import_path)
        except Exception as exc:
            messagebox.showerror('Импорт проекта',str(exc),parent=self)

    def svg(self):
        self._export("svg")

    def csv(self):
        self._export("csv")

    def materials(self):
        self._export("materials")

    def _export(self,ext):
        try:
            self.read_form()
            if self.workflow_stage not in ('lines','plates'):
                raise ValueError('Сначала нажмите «Рассчитать».')
            suffix = "csv" if ext=="materials" else ext
            path=filedialog.asksaveasfilename(parent=self,title="Экспорт",defaultextension="."+suffix,
                                              filetypes=[(suffix.upper(),"*."+suffix)])
            if path:
                if ext=="svg":export_svg(self.result,path,pipe_mode=self.pipe_mode.get(),
                                     show_well_guards=self.show_well_guards.get(),
                                     show_main_guards=self.show_main_guards.get())
                elif ext=="csv":export_csv(self.result,path)
                else:export_materials_csv(self.result,path)
                messagebox.showinfo("Готово",f"Файл создан:\n{path}",parent=self)
        except Exception as e:
            messagebox.showerror("Экспорт",str(e),parent=self)


if __name__ == "__main__":
    Editor().mainloop()
