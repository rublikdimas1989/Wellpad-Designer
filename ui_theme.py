"""Slate Blue: тёмные панели вокруг светлого инженерного чертежа.

Все настройки интерфейса локальные: ни JSON проекта, ни SVG-печать не меняются.
Tk/ttk не гарантирует нативные скругления на каждой платформе.
"""
from __future__ import annotations
from tkinter import ttk

PALETTE = {
    'window': '#0e1c2d',
    'panel': '#14283d',
    'card': '#19324a',
    'card_hover': '#23425e',
    'input': '#0d2235',
    'border': '#31536d',
    'text': '#e8f3fb',
    'muted': '#a7c1d2',
    'hint': '#82c5e8',
    'accent': '#068cbb',
    'accent_hover': '#10a6d2',
    'accent_pressed': '#0576a2',
    'select': '#126b92',
    'disabled': '#637c90',
    'warning': '#edc37c',
    'drawing': '#f0f7fc',
    'grid': '#dce9f3',
    'grid_major': '#bfd5e6',
    'drawing_border': '#adc6d9',
}


def apply_theme(root):
    c = PALETTE
    style = ttk.Style(root)
    if 'clam' in style.theme_names():
        style.theme_use('clam')
    root.configure(background=c['window'])
    style.configure('.', font=('Segoe UI', 10), background=c['window'], foreground=c['text'])
    style.configure('TFrame', background=c['panel'])
    style.configure('Card.TFrame', background=c['panel'])
    style.configure('TLabel', background=c['panel'], foreground=c['text'], font=('Segoe UI', 10))
    style.configure('Muted.TLabel', foreground=c['muted'])
    style.configure('Title.TLabel', foreground=c['text'], font=('Segoe UI', 14, 'bold'))
    style.configure('TLabelframe', background=c['panel'], bordercolor=c['border'], borderwidth=1,
                    relief='solid', padding=7)
    style.configure('TLabelframe.Label', background=c['panel'], foreground=c['hint'],
                    font=('Segoe UI', 10, 'bold'))
    style.configure('TButton', background=c['card'], foreground=c['text'],
                    bordercolor=c['border'], borderwidth=1, relief='flat',
                    padding=(11, 8), font=('Segoe UI', 10))
    style.map('TButton',
              background=[('disabled', c['panel']), ('pressed', c['accent_pressed']),
                          ('active', c['card_hover'])],
              foreground=[('disabled', c['disabled']), ('active', c['text'])],
              bordercolor=[('focus', c['accent']), ('active', c['hint'])])
    style.configure('Accent.TButton', background=c['accent'], foreground='#ffffff',
                    bordercolor=c['accent'], borderwidth=1, relief='flat',
                    font=('Segoe UI', 10, 'bold'), padding=(13, 9))
    style.map('Accent.TButton', background=[('disabled', c['panel']),
                  ('pressed', c['accent_pressed']), ('active', c['accent_hover'])],
              foreground=[('disabled', c['disabled']), ('active', '#ffffff')])
    style.configure('TEntry', fieldbackground=c['input'], background=c['input'],
                    foreground=c['text'], bordercolor=c['border'], lightcolor=c['border'],
                    darkcolor=c['border'], insertcolor=c['text'], padding=(7, 5))
    style.map('TEntry', fieldbackground=[('disabled', c['panel']), ('readonly', c['input'])],
              foreground=[('disabled', c['disabled']), ('readonly', c['text'])],
              bordercolor=[('focus', c['accent'])])
    style.configure('TCombobox', fieldbackground=c['input'], background=c['card'],
                    foreground=c['text'], arrowcolor=c['hint'], bordercolor=c['border'],
                    lightcolor=c['border'], darkcolor=c['border'], padding=(6, 5))
    style.map('TCombobox',
              fieldbackground=[('readonly', c['input']), ('focus', c['input'])],
              foreground=[('readonly', c['text']), ('disabled', c['disabled'])],
              selectbackground=[('readonly', c['input'])],
              selectforeground=[('readonly', c['text'])],
              background=[('active', c['card_hover'])],
              arrowcolor=[('active', '#ffffff')], bordercolor=[('focus', c['accent'])])
    root.option_add('*TCombobox*Listbox.background', c['input'])
    root.option_add('*TCombobox*Listbox.foreground', c['text'])
    root.option_add('*TCombobox*Listbox.selectBackground', c['select'])
    root.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')
    style.configure('TCheckbutton', background=c['panel'], foreground=c['text'], padding=(4, 4))
    style.map('TCheckbutton', background=[('active', c['panel'])],
              foreground=[('disabled', c['disabled'])])
    style.configure('TRadiobutton', background=c['panel'], foreground=c['text'])
    style.map('TRadiobutton', background=[('active', c['panel'])])
    style.configure('TScrollbar', background=c['card'], troughcolor=c['window'],
                    arrowcolor=c['muted'], bordercolor=c['window'])
    style.map('TScrollbar', background=[('active', c['card_hover'])])
    style.configure('Treeview', background=c['input'], fieldbackground=c['input'],
                    foreground=c['text'], borderwidth=0, rowheight=29,
                    font=('Segoe UI', 10))
    style.map('Treeview', background=[('selected', c['select'])],
              foreground=[('selected', '#ffffff')])
    style.configure('Treeview.Heading', background=c['panel'], foreground=c['muted'],
                    bordercolor=c['border'], relief='flat', padding=(5, 8),
                    font=('Segoe UI', 9, 'bold'))
    style.map('Treeview.Heading', background=[('active', c['card_hover'])])
    return style


def style_spinbox(spin):
    c = PALETTE
    spin.configure(background=c['input'], foreground=c['text'],
                   buttonbackground=c['card'], insertbackground=c['text'],
                   readonlybackground=c['input'], highlightthickness=1,
                   highlightbackground=c['border'], highlightcolor=c['accent'],
                   relief='flat', borderwidth=1)
    return spin
