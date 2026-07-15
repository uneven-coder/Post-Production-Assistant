@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Virtual environment not found at .venv\
    echo Set it up first with:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python main.py --silence-only
echo.
pause
