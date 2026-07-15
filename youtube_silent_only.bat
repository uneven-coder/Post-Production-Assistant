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

echo.
echo Copy a YouTube Studio video URL before running this - e.g.
echo https://studio.youtube.com/video/VIDEO_ID/edit
echo.

call .venv\Scripts\activate.bat
python main.py --youtube-silent-only
echo.
pause
