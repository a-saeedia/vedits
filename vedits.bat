@echo off
REM Vedits launcher (Windows). Usage: vedits <subcommand> [args]
set "ROOT=%~dp0"
set "PY=C:\Users\User\tools\litellm\.venv\Scripts\python.exe"
"%PY%" "%ROOT%run.py" %*
