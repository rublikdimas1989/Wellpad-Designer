"""ОТКЛЮЧЕНО (v91): совместная оптимизация внешних линий.

Внешние подключения (внеось / врезка / пучки) сняты для переработки.
Модуль оставлен как заглушка, чтобы старые импорты не ломали загрузку.
"""
def jointly_optimize(survey, catalog, initial, *, max_trials=35, progress=None):
    initial.notes.append("joint_network_optimizer отключён (v91).")
    return initial

def corridor_combinations(survey, catalog, selected, *, limit=32):
    return []
