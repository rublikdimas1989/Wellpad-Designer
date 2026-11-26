"""Стартовый экран и пошаговое создание нового куста. Только tkinter."""
from __future__ import annotations

import math
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, simpledialog

from routing import Frame, KIND_RU, STATUS_RU, Project, Pt, Well
from automatic_wells import locate_off_axis_well
from centering import center_window
from ui_theme import PALETTE, style_spinbox


KIND_BY_NAME = {value: key for key, value in KIND_RU.items()}
STATUS_BY_NAME = {value: key for key, value in STATUS_RU.items()}
MODE_LABELS = {
    'X — север, Y — восток (геодезические)': 'x_north',
    'X — восток, Y — север': 'x_east',
}


def number(value: str, label: str) -> float:
    value = value.strip().replace(' ', '').replace(',', '.')
    if not value:
        raise ValueError(f'Заполните поле «{label}».')
    try:
        out = float(value)
    except ValueError as exc:
        raise ValueError(f'Введите число в поле «{label}».') from exc
    if not math.isfinite(out):
        raise ValueError(f'Некорректное число в поле «{label}».')
    return out


class NewProjectWizard(tk.Toplevel):
    """Шаг 1: точка/координаты первой скважины; шаг 2: число и типы скважин."""

    def __init__(self, master, on_create, default_name: str):
        super().__init__(master)
        self.on_create = on_create
        self.title('Новый проект — исходные данные')
        self.geometry('1240x860')
        self.minsize(1080, 700)
        self.transient(master)
        self.name = tk.StringVar(value=default_name)
        self.first_x = tk.StringVar(value='100')
        self.first_y = tk.StringVar(value='100')
        self.azimuth = tk.StringVar(value='90')
        self.mode = tk.StringVar(value=list(MODE_LABELS)[0])
        self.ref_x = tk.StringVar(value='100')
        self.ref_y = tk.StringVar(value='100')
        self.count = tk.StringVar(value='10')
        self.well_rows: list[dict] = []
        self.step_index = 0
        self._map_marker: tuple[float, float] | None = None
        self._init_shell()
        self.build_step1()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Escape>', lambda e: self.close())
        self.after_idle(lambda:center_window(self))
        self._grab_job=self.after(100, self._activate_grab)

    def _activate_grab(self):
        self._grab_job=None
        try:
            if self.winfo_exists():
                self.grab_set()
        except tk.TclError:
            pass

    def _cancel_pending_grab(self):
        if getattr(self,'_grab_job',None) is not None:
            try:
                self.after_cancel(self._grab_job)
            except tk.TclError:
                pass
            self._grab_job=None

    def _init_shell(self):
        self.configure(background=PALETTE['window'])
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        header = ttk.Frame(self, padding=(24, 16))
        header.grid(row=0, column=0, sticky='ew')
        ttk.Label(header, text='СОЗДАНИЕ ПРОЕКТА КУСТОВОЙ ПЛОЩАДКИ', font=('Segoe UI', 16, 'bold')).pack(anchor='w')
        self.step_title = tk.StringVar()
        ttk.Label(header, textvariable=self.step_title, foreground=PALETTE["hint"], font=('Segoe UI', 11)).pack(anchor='w', pady=(5, 0))
        self.body = ttk.Frame(self, padding=(24, 2, 24, 4))
        self.body.grid(row=1, column=0, sticky='nsew')
        footer = ttk.Frame(self, padding=18)
        footer.grid(row=2, column=0, sticky='ew')
        self.back_button = ttk.Button(footer, text='← Назад', command=self.build_step1)
        self.back_button.pack(side='left')
        ttk.Button(footer, text='Отмена', command=self.close).pack(side='right', padx=6)
        self.forward_button = ttk.Button(footer, text='Далее →',style='Accent.TButton', command=self.to_step2)
        self.forward_button.pack(side='right', padx=6)

    def clear_body(self):
        for widget in self.body.winfo_children():
            widget.destroy()

    def build_step1(self):
        self.step_index = 1
        self.step_title.set('ШАГ 1 ИЗ 2 — ПЕРВАЯ СКВАЖИНА И НАПРАВЛЕНИЕ НДС')
        self.clear_body()
        self.back_button.configure(state='disabled')
        self.forward_button.configure(text='Далее: скважины →', command=self.to_step2)
        frm = ttk.Frame(self.body)
        frm.pack(fill='x')
        frm.columnconfigure(1, weight=1)
        fields = (
            ('Название проекта', self.name),
            ('Координата X первой скважины, м', self.first_x),
            ('Координата Y первой скважины, м', self.first_y),
            ('Дирекционный угол оси НДС, °', self.azimuth),
        )
        for i, (label, value) in enumerate(fields):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky='w', pady=5, padx=(0, 16))
            ttk.Entry(frm, textvariable=value, width=34).grid(row=i, column=1, sticky='ew', pady=5)
        ttk.Label(frm, text='Координатная система').grid(row=4, column=0, sticky='w', pady=5)
        ttk.Combobox(frm, textvariable=self.mode, values=list(MODE_LABELS), state='readonly').grid(row=4, column=1, sticky='ew', pady=5)
        ttk.Label(self.body, text='Можно ввести X/Y вручную или выбрать точку на инженерной координатной сетке ниже.',
                  foreground=PALETTE["muted"]).pack(fill='x', pady=(10, 6))
        map_frame = ttk.LabelFrame(self.body, text='Указать первую скважину щелчком по плану', padding=8)
        map_frame.pack(fill='both', expand=True)
        controls = ttk.Frame(map_frame)
        controls.pack(fill='x')
        ttk.Label(controls, text='X центра сетки:').pack(side='left')
        ttk.Entry(controls, textvariable=self.ref_x, width=14).pack(side='left', padx=5)
        ttk.Label(controls, text='Y центра сетки:').pack(side='left', padx=(14, 0))
        ttk.Entry(controls, textvariable=self.ref_y, width=14).pack(side='left', padx=5)
        ttk.Button(controls, text='Обновить сетку', command=self.redraw_map).pack(side='right', padx=3)
        ttk.Button(controls, text='Центрировать на X/Y', command=self.center_map_on_well).pack(side='right', padx=3)
        self.map_canvas = tk.Canvas(map_frame, background=PALETTE['drawing'], height=345,
                                    highlightbackground=PALETTE['drawing_border'], highlightthickness=1, cursor='crosshair')
        self.map_canvas.pack(fill='both', expand=True, pady=(8, 5))
        self.map_canvas.bind('<Configure>', lambda event: self.redraw_map())
        self.map_canvas.bind('<Button-1>', self.choose_on_map)
        ttk.Label(map_frame, text='Это схематичный 2D-план, не спутниковая карта. Центр сетки задаётся X/Y выше; шаг 10 м.',
                  foreground=PALETTE["muted"]).pack(anchor='w')
        self.map_hint = tk.StringVar(value='Щёлкните по сетке — выбранные координаты заполнят X и Y первой скважины.')
        ttk.Label(map_frame, textvariable=self.map_hint, foreground=PALETTE["hint"]).pack(anchor='w', pady=(3, 0))
        self.after_idle(self.redraw_map)

    def center_map_on_well(self):
        try:
            self.ref_x.set(self.first_x.get())
            self.ref_y.set(self.first_y.get())
            number(self.ref_x.get(), 'X центра сетки')
            number(self.ref_y.get(), 'Y центра сетки')
            self.redraw_map()
        except ValueError as exc:
            messagebox.showerror('Координаты', str(exc), parent=self)

    def _map_dimensions(self):
        return max(360, self.map_canvas.winfo_width()), max(220, self.map_canvas.winfo_height())

    def _click_to_xy(self, ex: float, ey: float, width: float, height: float) -> tuple[float, float]:
        rx = number(self.ref_x.get(), 'X центра сетки')
        ry = number(self.ref_y.get(), 'Y центра сетки')
        east = (ex-width/2)/5
        north = (height/2-ey)/5
        if MODE_LABELS[self.mode.get()] == 'x_north':
            return rx+north, ry+east
        return rx+east, ry+north

    def choose_on_map(self, event):
        try:
            w, h = self._map_dimensions()
            x, y = self._click_to_xy(event.x, event.y, w, h)
            self.first_x.set(f'{x:.3f}')
            self.first_y.set(f'{y:.3f}')
            self._map_marker = (event.x, event.y)
            self.map_hint.set(f'Выбрана скважина №1: X={x:.3f}  Y={y:.3f}')
            self.redraw_map()
        except ValueError as exc:
            messagebox.showerror('Центр сетки', str(exc), parent=self)

    def redraw_map(self):
        if not hasattr(self, 'map_canvas') or not self.map_canvas.winfo_exists():
            return
        c = self.map_canvas
        c.delete('all')
        w, h = self._map_dimensions()
        scale = 50
        for i in range(-math.ceil(w/scale), math.ceil(w/scale)+1):
            x=w/2+i*scale
            c.create_line(x, 0, x, h, fill=PALETTE['grid'])
        for i in range(-math.ceil(h/scale), math.ceil(h/scale)+1):
            y=h/2+i*scale
            c.create_line(0, y, w, y, fill=PALETTE['grid'])
        c.create_line(0,h/2,w,h/2,fill=PALETTE['grid_major'],width=2)
        c.create_line(w/2,0,w/2,h,fill=PALETTE['grid_major'],width=2)
        c.create_text(w/2+8,h/2+14,text='Центр координатной сетки',anchor='nw',fill='#738592')
        c.create_text(w-14,15,text='СЕВЕР ↑',anchor='ne',fill='#2d4655',font=('Segoe UI',11,'bold'))
        if self._map_marker is not None:
            x, y = self._map_marker
            c.create_oval(x-7,y-7,x+7,y+7,outline='#c1364d',width=2,fill='white')
            c.create_line(x-11,y,x+11,y,fill='#c1364d',width=1)
            c.create_line(x,y-11,x,y+11,fill='#c1364d',width=1)
            c.create_text(x+12,y-14,text='СКВ. №1',anchor='sw',fill='#a02b41',font=('Segoe UI',10,'bold'))
        try:
            rx=number(self.ref_x.get(), 'X центра')
            ry=number(self.ref_y.get(), 'Y центра')
            c.create_text(10,h-12,text=f'Центр: X={rx:.3f} Y={ry:.3f} | 1 клетка = 10 м',anchor='sw',fill='#536e7d')
        except ValueError:
            pass

    def to_step2(self):
        try:
            if not self.name.get().strip():
                raise ValueError('Введите название проекта.')
            number(self.first_x.get(), 'X первой скважины')
            number(self.first_y.get(), 'Y первой скважины')
            az = number(self.azimuth.get(), 'Азимут НДС')
            if not 0 <= az <= 360:
                raise ValueError('Азимут должен быть от 0 до 360°.')
            if self.mode.get() not in MODE_LABELS:
                raise ValueError('Выберите систему координат.')
        except ValueError as exc:
            messagebox.showerror('Исходные данные', str(exc), parent=self)
            return
        self.build_step2()

    def build_step2(self):
        self.step_index=2
        self.step_title.set('ШАГ 2 ИЗ 2 — КОЛИЧЕСТВО, ТИП И СТАТУС СКВАЖИН')
        self.clear_body()
        self.back_button.configure(state='normal')
        self.forward_button.configure(text='Создать проект', command=self.create)
        header = ttk.Frame(self.body)
        header.pack(fill='x', pady=(4, 10))
        ttk.Label(header, text='Количество скважин (1–100):').pack(side='left')
        style_spinbox(tk.Spinbox(header, from_=1, to=100, width=7, textvariable=self.count)).pack(side='left', padx=8)
        ttk.Button(header, text='Применить количество', command=self.render_well_rows).pack(side='left')
        ttk.Label(self.body, text='При снятии галочки «На оси» программа подбирает X/Y, проверяя 5-метровую зону относительно нефтепровода к врезке. Координаты можно изменить вручную. Первая скважина всегда задаёт начало оси.',
                  foreground=PALETTE["muted"], wraplength=1060).pack(fill='x', pady=(0, 10))
        table_header = ttk.Frame(self.body)
        table_header.pack(fill='x')
        for label,width in (('№',5),('Назначение',25),('Статус',18),('На оси',9),('X',15),('Y',15),('Длинная сторона',20),('Закрепить',10)):
            ttk.Label(table_header,text=label,width=width).pack(side='left')
        list_frame = ttk.Frame(self.body)
        list_frame.pack(fill='both', expand=True)
        canvas = tk.Canvas(list_frame, background=PALETTE['panel'], highlightthickness=0)
        scroll = ttk.Scrollbar(list_frame, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        holder = ttk.Frame(canvas, padding=(5,5))
        window = canvas.create_window((0,0), window=holder, anchor='nw')
        holder.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))
        self.rows_canvas=canvas
        self.rows_holder=holder
        self.render_well_rows()
        ttk.Label(self.body,text='«Закрепить» = не разрешать оптимизатору автоматически поворачивать эту скважину. Если не отмечено, ориентация может изменяться при расчёте.',
                  foreground=PALETTE["muted"],wraplength=1000).pack(fill='x',pady=(7,2))
        ttk.Label(self.body,text='По умолчанию точка врезки X=200; Y=90 (координаты можно изменить). Пожарный узел размещён ориентировочно. При создании проекта маршруты не рассчитываются.',
                  foreground=PALETTE["warning"],wraplength=840).pack(fill='x',pady=(9,5))

    def render_well_rows(self):
        try:
            n=int(self.count.get())
            if not 1<=n<=100:raise ValueError('Допустимо от 1 до 100 скважин.')
        except ValueError as exc:
            messagebox.showerror('Количество',str(exc),parent=self)
            return
        old=[]
        for item in self.well_rows:
            old.append({k:var.get() for k,var in item.items() if k not in ('x_entry','y_entry')})
        self.well_rows=[]
        for child in self.rows_holder.winfo_children():child.destroy()
        for i in range(n):
            previous=old[i] if i<len(old) else {}
            fields={
                'kind':tk.StringVar(value=previous.get('kind',('Добывающая' if i<5 else 'Нагнетательная' if i<7 else 'Водозаборная ППД' if i<9 else 'Водозаборная СППВ' if i==9 else 'Добывающая'))),
                'status':tk.StringVar(value=previous.get('status','Обустраиваемая')),
                'on_axis':tk.BooleanVar(value=previous.get('on_axis',False if i in (8,9) else True)),
                'x':tk.StringVar(value=previous.get('x','150' if i==8 else '130' if i==9 else '')),
                'y':tk.StringVar(value=previous.get('y','70' if i==8 else '50' if i==9 else '')),
                'orientation':tk.StringVar(value=previous.get('orientation','Длинная вдоль НДС')),
                'locked':tk.BooleanVar(value=previous.get('locked',False)),
            }
            if i==0: fields['on_axis'].set(True)
            row=ttk.Frame(self.rows_holder,padding=(2,5));row.pack(fill='x')
            ttk.Label(row,text=str(i+1),width=5).pack(side='left')
            ttk.Combobox(row,textvariable=fields['kind'],values=list(KIND_BY_NAME),state='readonly',width=23).pack(side='left')
            ttk.Combobox(row,textvariable=fields['status'],values=list(STATUS_BY_NAME),state='readonly',width=17).pack(side='left',padx=(6,0))
            cb=ttk.Checkbutton(row,variable=fields['on_axis'])
            cb.pack(side='left',padx=(16,18))
            if i==0:cb.configure(state='disabled')
            for key in ('x','y'):
                fields[key+'_entry']=ttk.Entry(row,textvariable=fields[key],width=15)
                fields[key+'_entry'].pack(side='left',padx=(1,3))
            ttk.Combobox(row,textvariable=fields['orientation'],values=('Длинная вдоль НДС','Длинная поперёк НДС'),state='readonly',width=24).pack(side='left',padx=(3,5))
            ttk.Checkbutton(row,variable=fields['locked'],text='Да').pack(side='left',padx=9)
            def toggle(*_unused, f=fields, idx=i):
                state='disabled' if f['on_axis'].get() else 'normal'
                for key in ('x_entry','y_entry'):f[key].configure(state=state)
                if state=='normal' and not f['x'].get().strip() and not f['y'].get().strip():
                    try:
                        draft=self._draft_for_autoplacement(idx, f)
                        wx,wy=locate_off_axis_well(draft, idx+1)
                        f['x'].set(f'{wx:.3f}')
                        f['y'].set(f'{wy:.3f}')
                    except (ValueError, KeyError) as exc:
                        # Не выдавать старые координаты, которые могли попасть
                        # в защитную зону подхода продуктовой нефти к врезке.
                        messagebox.showwarning('Автоматическое размещение',
                            str(exc)+'\nУкажите координаты X/Y вручную.', parent=self)
            fields['on_axis'].trace_add('write',toggle)
            toggle()
            self.well_rows.append(fields)

    def _draft_for_autoplacement(self, index, current):
        """Собрать полный черновик в момент снятия галочки «На оси».

        Для ещё не заполненных строк создаются обычные осевые скважины;
        уже введённые вручную внеосевые координаты остальных сохраняются.
        """
        n=int(self.count.get())
        if not 1<=n<=100:
            raise ValueError('Количество скважин должно быть от 1 до 100.')
        x=number(self.first_x.get(),'X первой скважины')
        y=number(self.first_y.get(),'Y первой скважины')
        az=number(self.azimuth.get(),'Азимут НДС')
        if not 0<=az<=360:
            raise ValueError('Азимут НДС должен быть от 0 до 360°.')
        wells=[]
        for i in range(n):
            row=current if i==index else (self.well_rows[i] if i<len(self.well_rows) else None)
            kind=(KIND_BY_NAME.get(row['kind'].get(),'producer') if row else
                  ('producer' if i<5 else 'injector' if i<7 else
                   'ppd_source' if i<9 else 'fresh_source' if i==9 else 'producer'))
            status=STATUS_BY_NAME.get(row['status'].get(),'active') if row else 'active'
            orientation=('perpendicular' if row and row['orientation'].get()=='Длинная поперёк НДС'
                         else 'parallel')
            on_axis=True
            wx=wy=None
            if row and i!=index and not row['on_axis'].get():
                # Учитываем все уже заполненные внеосевые скважины.
                if row['x'].get().strip() and row['y'].get().strip():
                    wx=number(row['x'].get(),f'X скважины №{i+1}')
                    wy=number(row['y'].get(),f'Y скважины №{i+1}')
                    on_axis=False
            wells.append(Well(number=i+1,kind=kind,status=status,on_axis=on_axis,
                              x=wx,y=wy,orientation=orientation,
                              rotation_locked=bool(row['locked'].get()) if row else False))
        project=Project(first_x=x,first_y=y,azimuth=az,
                        coordinate_mode=MODE_LABELS[self.mode.get()], wells=wells,
                        tie_confirmed=True,fire_confirmed=False)
        frame=Frame(project)
        project.tie_x,project.tie_y=200.0,90.0
        project.fire_x,project.fire_y=frame.xy(Pt(-project.head_lead-35,
                                                  project.offset+3*project.lane_gap))
        project.validate()
        return project

    def create(self):
        try:
            x=number(self.first_x.get(), 'X первой скважины')
            y=number(self.first_y.get(), 'Y первой скважины')
            az=number(self.azimuth.get(), 'Азимут')
            self.render_well_rows_if_needed()
            wells=[]
            for i,row in enumerate(self.well_rows):
                on_axis=row['on_axis'].get()
                x2=None if on_axis else number(row['x'].get(),f'X скважины №{i+1}')
                y2=None if on_axis else number(row['y'].get(),f'Y скважины №{i+1}')
                orientation=('parallel' if row['orientation'].get()=='Длинная вдоль НДС' else 'perpendicular')
                wells.append(Well(i+1,KIND_BY_NAME[row['kind'].get()],STATUS_BY_NAME[row['status'].get()],
                                  on_axis,x2,y2,False,orientation,row['locked'].get()))
            project=Project(first_x=x,first_y=y,azimuth=az,coordinate_mode=MODE_LABELS[self.mode.get()],wells=wells)
            # Точные внешние точки пока не вводятся. Размещаем временные координаты
            # относительно скважины №1, обязательно отмечаем отсутствие подтверждения.
            frame=Frame(project)
            # ЭТАП «СОЗДАТЬ ПРОЕКТ»: только исходные данные и простая геометрия.
            # Не вызывать routing.build(): глобальный перебор может занимать
            # значительное время и блокировать окно Tkinter при создании.
            # Точка врезки имеет фиксированный стандартный X=200, Y=90.
            # Пожарный узел остаётся предварительным.
            project.tie_x,project.tie_y=200.0,90.0
            project.fire_x,project.fire_y=frame.xy(Pt(-project.head_lead-35, project.offset+3*project.lane_gap))
            project.tie_confirmed=True
            project.fire_confirmed=False
            project.validate()  # Быстрая проверка полей, без маршрутизации.
            name=self.name.get().strip()
        except (KeyError, ValueError) as exc:
            messagebox.showerror('Создание проекта', str(exc), parent=self)
            return
        self._cancel_pending_grab()
        self.grab_release()
        self.destroy()
        self.on_create(name,project)

    def render_well_rows_if_needed(self):
        n=int(self.count.get())
        if not 1<=n<=100:
            raise ValueError('Допустимо от 1 до 100 скважин.')
        if n != len(self.well_rows):
            self.render_well_rows()

    def close(self):
        self._cancel_pending_grab()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


class ProjectHome(tk.Toplevel):
    """Стартовый экран: список проектов, создание, открытие, импорт и удаление."""

    def __init__(self, app, store):
        super().__init__(app)
        self.app = app
        self.store = store
        self.title('Проектировщик кустовых площадок — Хребет v93c — Мои проекты')
        self.configure(background=PALETTE['window'])
        self.geometry('1050x670')
        self.after_idle(lambda:center_window(self))
        self.minsize(800,550)
        self.protocol('WM_DELETE_WINDOW', self.exit_app)
        self._items = {}
        self.build()
        self.refresh()

    def build(self):
        background=ttk.Frame(self,padding=26)
        background.pack(fill='both',expand=True)
        ttk.Label(background,text='МОИ ПРОЕКТЫ',font=('Segoe UI',23,'bold'),foreground=PALETTE["text"]).pack(anchor='w')
        ttk.Label(background,text='Кустовые площадки  ·  Хребет v93c  ·  Вид сверху  ·  Без плит',
                  foreground=PALETTE["muted"],font=('Segoe UI',11)).pack(anchor='w',pady=(4,22))
        commands=ttk.Frame(background)
        commands.pack(fill='x',pady=(0,17))
        ttk.Button(commands,text='＋  Создать новый проект',style='Accent.TButton',command=self.new).pack(side='left',padx=(0,8))
        ttk.Button(commands,text='Открыть выбранный',command=self.open_selected).pack(side='left',padx=(0,8))
        ttk.Button(commands,text='Импортировать старый .json',command=self.import_old).pack(side='left')
        ttk.Button(commands,text='Обновить',command=self.refresh).pack(side='right')
        holder=ttk.Frame(background)
        holder.pack(fill='both',expand=True)
        self.tree=ttk.Treeview(holder,columns=('name','wells','angle','date'),show='headings',selectmode='browse')
        for key,title,width in (('name','Название проекта',430),('wells','Скважин',95),('angle','Азимут НДС',130),('date','Последнее сохранение',220)):
            self.tree.heading(key,text=title)
            self.tree.column(key,width=width,anchor='w' if key=='name' else 'center',stretch=(key=='name'))
        scroll=ttk.Scrollbar(holder,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left',fill='both',expand=True)
        scroll.pack(side='right',fill='y')
        self.tree.bind('<Double-1>',lambda e:self.open_selected())
        self.tree.bind('<Return>',lambda e:self.open_selected())
        bottom=ttk.Frame(background)
        bottom.pack(fill='x',pady=(16,0))
        self.caption=tk.StringVar(value='')
        ttk.Label(bottom,textvariable=self.caption,foreground=PALETTE["muted"]).pack(side='left')
        ttk.Button(bottom,text='Удалить выбранный',command=self.delete_selected).pack(side='right',padx=(8,0))
        ttk.Button(bottom,text='Переименовать',command=self.rename_selected).pack(side='right',padx=(8,0))
        ttk.Button(bottom,text='Выход',command=self.exit_app).pack(side='right')
        ttk.Label(background,text='Проекты хранятся локально в папке «Проекты» рядом с программой. Для открытия нажмите дважды по строке.',
                  foreground=PALETTE["muted"]).pack(anchor='w',pady=(18,0))

    def refresh(self):
        self._items={}
        for child in self.tree.get_children():
            self.tree.delete(child)
        for i,item in enumerate(self.store.list_items()):
            key=str(i)
            self._items[key]=item
            self.tree.insert('', 'end', iid=key,values=(item.name,item.wells,f'{item.azimuth:g}°',item.modified))
        self.caption.set(f'Проектов: {len(self._items)}' if self._items else 'Список пуст — нажмите «Создать новый проект».')

    def selected_item(self):
        selected=self.tree.selection()
        if not selected:
            messagebox.showinfo('Проекты','Сначала выберите проект в списке.',parent=self)
            return None
        return self._items.get(selected[0])

    def new(self):
        NewProjectWizard(self,self._created,self.store.next_name())

    def _created(self,name,project):
        self.app.create_managed_project(name,project)

    def open_selected(self):
        item=self.selected_item()
        if item:
            self.app.open_managed_project(item.path)

    def import_old(self):
        source=filedialog.askopenfilename(parent=self,title='Импортировать проект JSON',filetypes=[('Проект JSON','*.json')])
        if not source:
            return
        try:
            project=Project.load(source)
            name=Path(source).stem
            path=self.store.create(name,project)
            self.refresh()
            self.app.open_managed_project(path)
        except Exception as exc:
            messagebox.showerror('Ошибка импорта',str(exc),parent=self)

    def rename_selected(self):
        item=self.selected_item()
        if item is None:
            return
        name=simpledialog.askstring('Переименование', 'Новое название проекта:',
                                    initialvalue=item.name, parent=self)
        if name is None:
            return
        name=name.strip()
        if not name:
            messagebox.showerror('Переименование','Название не может быть пустым.',parent=self)
            return
        try:
            project=self.store.load(item.path)
            self.store.save(item.path,project,name)
            self.refresh()
        except Exception as exc:
            messagebox.showerror('Переименование',str(exc),parent=self)

    def delete_selected(self):
        item=self.selected_item()
        if item is None:
            return
        if not messagebox.askyesno('Удаление проекта',f'Удалить проект «{item.name}»?\nФайл будет удалён без корзины.',parent=self):
            return
        try:
            self.store.delete(item.path)
            self.refresh()
        except Exception as exc:
            messagebox.showerror('Удаление',str(exc),parent=self)

    def exit_app(self):
        self.app.destroy()
