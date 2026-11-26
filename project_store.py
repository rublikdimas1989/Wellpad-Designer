"""Локальный реестр проектов: файлы JSON в папке «Проекты» рядом с приложением.

Не требует интернет-соединения или базы данных. Сохранение атомарное, чтобы
случайное закрытие приложения не обрывало основной файл.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import uuid

from routing import Project


@dataclass(frozen=True)
class ProjectItem:
    path: Path
    name: str
    wells: int
    modified: str
    azimuth: float


class ProjectStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _check(self, path: Path) -> Path:
        path = Path(path).resolve()
        if path.parent != self.directory.resolve() or path.suffix.lower() != '.json':
            raise ValueError('Проект должен находиться в папке «Проекты».')
        return path

    def list_items(self) -> list[ProjectItem]:
        result = []
        for path in self.directory.glob('*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                # В списке только корректные проекты, неправильные файлы не удаляем.
                p = Project.from_dict(data)
                name = str(data.get('_name') or path.stem).strip()
                result.append(ProjectItem(path, name, len(p.wells),
                                          datetime.fromtimestamp(path.stat().st_mtime).strftime('%d.%m.%Y %H:%M'),
                                          p.azimuth))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
        result.sort(key=lambda item: item.path.stat().st_mtime, reverse=True)
        return result

    def create(self, name: str, project: Project) -> Path:
        name = name.strip()
        if not name:
            raise ValueError('Укажите название проекта.')
        if len(name) > 120:
            raise ValueError('Название должно быть короче 120 символов.')
        project.validate()
        path = self.directory / f'project_{uuid.uuid4().hex}.json'
        self.save(path, project, name)
        return path

    def save(self, path: Path, project: Project, name: str | None = None) -> None:
        path = self._check(path)
        project.validate()
        if name is None and path.exists():
            original = json.loads(path.read_text(encoding='utf-8'))
            name = str(original.get('_name') or path.stem)
        name = (name or path.stem).strip()
        if not name:
            raise ValueError('Имя проекта пустое.')
        data = {'version': 87, '_name': name, **asdict(project)}
        temp = path.with_name(f'.{path.stem}_{uuid.uuid4().hex}.tmp')
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    def load(self, path: Path) -> Project:
        file_path=self._check(path)
        data=json.loads(file_path.read_text(encoding='utf-8'))
        project=Project.from_dict(data)
        # Миграция рабочего редактора: в старых версиях часто сохранялась
        # отключённая оптимизация положения. С v56 бесплатный поворот
        # незакреплённых внеосевых скважин оценивается по умолчанию.
        # Явное отключение, сохранённое уже в v56, сохраняется.
        if int(data.get('version',0))<56 and any(not w.on_axis and not w.rotation_locked
                                                 for w in project.wells):
            project.auto_rotate=True
        return project

    def delete(self, path: Path) -> None:
        self._check(path).unlink()

    def next_name(self) -> str:
        used = {item.name for item in self.list_items()}
        i = 1
        while f'Куст №{i}' in used:
            i += 1
        return f'Куст №{i}'
