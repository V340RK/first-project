@echo off
REM V340RK dashboard — подвійний клік для запуску.
REM Закрий це вікно, щоб зупинити сервер.

cd /d "%~dp0"

echo ========================================
echo   V340RK Dashboard
echo   http://127.0.0.1:8765/app
echo ========================================
echo.
echo Браузер відкриється через 3 секунди.
echo Закрий це вікно, щоб зупинити бота.
echo.

start "" cmd /c "timeout /T 3 /NOBREAK >nul && start http://127.0.0.1:8765/app"

.venv\Scripts\python.exe -m scalper.dashboard --port 8765

echo.
echo Сервер зупинився.
pause
