@echo off
setlocal enabledelayedexpansion
title LyricForge Server
cd /d "%~dp0"

:: UTF-8 so non-English titles and lyrics print cleanly in the console.
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

:: WhisperX imports pyannote/spacy; these keep its generated protobuf code and
:: the TensorFlow shim from breaking the import on this machine.
set "USE_TF=0"
set "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python"

:: Up to three songs render at once. Stem separation and lyric alignment
:: each keep a slot of their own, so two songs can be in different stages.
if not defined LYRIC_JOB_CONCURRENCY set "LYRIC_JOB_CONCURRENCY=3"
if not defined LYRIC_STEM_CONCURRENCY set "LYRIC_STEM_CONCURRENCY=1"
if not defined LYRIC_ALIGN_CONCURRENCY set "LYRIC_ALIGN_CONCURRENCY=1"

echo =========================================
echo       Starting LyricForge Server...
echo =========================================

:: Check if virtual environment exists
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found!
    echo Create it and install dependencies first:
    echo     py -3.9 -m venv venv
    echo     venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

:: Verify the alignment stack before starting. A missing WhisperX, torch,
:: faster-whisper, Demucs, langdetect, or an incompatible numpy/pandas makes
:: un-timed lyrics fall back to Aeneas without the user knowing why.
echo Checking alignment dependencies...
.\venv\Scripts\python.exe -c "import numpy,pandas,langdetect,whisperx,faster_whisper,torch,torchaudio,demucs" >nul 2>&1
if errorlevel 1 (
    echo [WARN] The WhisperX alignment stack is missing or out of date.
    echo The app can still start, but un-timed lyrics may fall back to Aeneas.
    choice /c YN /n /m "Install/update dependencies from requirements.txt now? [Y/N] "
    if errorlevel 2 (
        echo Continuing without installing.
    ) else (
        echo Installing dependencies...
        .\venv\Scripts\python.exe -m pip install -r requirements.txt
        if errorlevel 1 (
            echo [ERROR] Dependency installation failed. See the output above.
            pause
            exit /b 1
        )
        .\venv\Scripts\python.exe -c "import numpy,pandas,langdetect,whisperx,faster_whisper,torch,torchaudio,demucs" >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] The alignment stack still does not import. See the errors above.
            pause
            exit /b 1
        )
    )
)

:: Find an available port
echo Looking for an open port...
for /f "delims=" %%a in ('.\venv\Scripts\python.exe -c "import socket; s=socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1]); s.close()"') do set "PORT=%%a"
if not defined PORT set "PORT=8000"

echo Found open port: %PORT%

:: Open the browser automatically
echo Opening browser at http://localhost:%PORT%...
start "" http://localhost:%PORT%

:: Start the FastAPI server using the virtual environment's python
echo Starting Uvicorn on port %PORT%...
.\venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port %PORT%

:: Keep window open if the server crashes or stops
pause
