@echo off
REM Launches Koli from the editable install.
REM Double-click this file, or pin its shortcut to taskbar/start menu.
REM Uses the project's .venv so it works outside an activated terminal too.
cd /d "%~dp0"
".venv\Scripts\python.exe" run-koli.py
