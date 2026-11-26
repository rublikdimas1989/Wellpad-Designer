@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if not errorlevel 1 (
    python main.py
    goto done
)
py -3 main.py
:done
if errorlevel 1 (
    echo.
    echo Ошибка запуска. Проверьте Python и содержимое папки.
    pause
)
