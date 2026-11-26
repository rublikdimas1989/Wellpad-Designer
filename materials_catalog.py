"""Справочник труб, извлечённый из «Калькулятор 28 v3 (1).xlsm».

Это справочный подбор для предварительной ведомости, а не проверка по
гидравлике, прочности или нормативам. Никакой зависимости от типовых МИС нет.
Арматура и соединительные детали в ведомость не включаются.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import csv

@dataclass(frozen=True)
class Pipe:
    key: str
    label: str
    diameter_mm: int
    wall_mm: int
    steel: str
    kg_per_m: float
    system: str
    source: str

PIPES = [
    Pipe("oil_159", "Труба ∅159х8,0 09Г2С", 159, 8, "09Г2С", 29.8, "Нефть", "Материал Нефть!H3:M3"),
    Pipe("oil_114", "Труба ∅114х9,0 09Г2С", 114, 9, "09Г2С", 23.3, "Нефть", "Материал Нефть!H4:M4"),
    Pipe("oil_89", "Труба ∅89х6,0 09Г2С", 89, 6, "09Г2С", 12.28, "Нефть", "Материал Нефть!H23:M23"),
    Pipe("ppd_159", "Труба ∅159х16,0 09Г2С", 159, 16, "09Г2С", 56.4, "ППД", "Материал ВВД!H3:M3"),
    Pipe("ppd_114", "Труба ∅114х12,0 09Г2С", 114, 12, "09Г2С", 30.2, "ППД", "Материал ВВД!H4:M4"),
    Pipe("ppd_57", "Труба ∅57х8,0 В 09Г2С", 57, 8, "09Г2С", 9.7, "ППД", "Материал ВВД!H5:M5"),
    Pipe("ppd_32", "Труба ∅32х6,0 В 09Г2С", 32, 6, "09Г2С", 3.9, "ППД", "Материал ВВД!H6:M6"),
    Pipe("fresh_57", "Труба ∅57х6,0 В 09Г2С", 57, 6, "09Г2С", 7.6, "СППВ", "Материал СППВ!H3:M3"),
]
INDEX = {item.key: item for item in PIPES}

# Опоры — только справочные позиции, их количества по одному метражу считать нельзя.
SUPPORTS = [
    ("Опора 57 09Г2С", 2.0, "Материал Общий!H3:M3"),
    ("Опора 89 09Г2С", 2.2, "Материал Общий!H4:M4"),
    ("Опора 114 09Г2С", 5.4, "Материал Общий!H5:M5"),
    ("Опора 159 09Г2С", 6.9, "Материал Общий!H6:M6"),
    ("Опора 57-ХБ-А-09Г2С", 0.3, "Материал Общий!H7:M7"),
    ("Опора 89-ХБ-А-09Г2С", 0.4, "Материал Общий!H8:M8"),
    ("Опора 133-ХБ-А-09Г2С", 1.0, "Материал Общий!H9:M9"),
    ("Опора 159-ХБ-А-09Г2С", 1.8, "Материал Общий!H10:M10"),
]


def pipe_for_segment(segment):
    """УСЛОВНЫЙ подбор при отсутствии гидравлики и детализации узлов."""
    if segment.medium == "gas":
        return None
    if segment.medium == "fresh":
        return INDEX["fresh_57"]
    if segment.medium == "oil_product":
        return INDEX["oil_159"]
    if segment.medium == "oil_measure":
        return INDEX["oil_114"]
    if segment.medium == "pressure":
        # Хребет / внешнее подключение и ветви различаем предварительно.
        if ("скв." in segment.purpose.lower() or
                "отвод" in segment.purpose.lower() or
                "пожар" in segment.purpose.lower()):
            return INDEX["ppd_114"]
        return INDEX["ppd_159"]
    return None


def takeoff(result):
    """Строки ведомости; не включают арматуру, отводы, тройники, покрытия, опоры."""
    from routing import MEDIA_RU
    amounts = {}
    for seg in result.segments:
        pipe = pipe_for_segment(seg)
        key = (seg.medium, seg.phase, pipe.key if pipe else None)
        amounts[key] = amounts.get(key, 0) + seg.length
    # В v26 пересечения лишь обозначаются на 2D-плане. Никаких
    # расчётных подъёмов/отводов и искусственных надбавок к метражу нет.
    rows = []
    for (medium, phase, code), length in sorted(amounts.items()):
        pipe = INDEX.get(code)
        rows.append({
            "Среда": MEDIA_RU[medium], "Состояние": "Перспектива" if phase == "future" else "Строительство",
            "Труба (предварительно)": pipe.label if pipe else "Не определена — нет данных о трубах газа",
            "Диаметр наружный, мм": pipe.diameter_mm if pipe else "",
            "Толщина стенки, мм": pipe.wall_mm if pipe else "",
            "Сталь": pipe.steel if pipe else "",
            "Длина, м": round(length, 3),
            "Масса погонная, кг/м": pipe.kg_per_m if pipe else "",
            "Масса труб, кг": round(length * pipe.kg_per_m, 2) if pipe else "",
            "Источник": pipe.source if pipe else "В калькуляторе нет отдельного справочника газовых труб",
            "Примечание": "Эскизный справочный подбор; проверить гидравлику, давление и прочность. Количество осевых плит — в ведомости проекта; масса и стоимость плит, арматура не рассчитаны"
        })
    return rows


def export_materials_csv(result, path):
    rows = takeoff(result)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["Среда", "Состояние", "Длина, м"], delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def export_catalog_csv(path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f,delimiter=";")
        w.writerow(["Наименование", "Система", "Наружный диаметр, мм", "Стенка, мм", "Сталь", "кг/м", "Откуда в Excel"])
        for p in PIPES:
            w.writerow([p.label,p.system,p.diameter_mm,p.wall_mm,p.steel,p.kg_per_m,p.source])
        w.writerow([])
        w.writerow(["Опоры (только справочник, без автоматического подсчёта)","","","","","масса 1 шт., кг","Откуда в Excel"])
        for name, mass, source in SUPPORTS:
            w.writerow([name,"Опоры","","","",mass,source])
