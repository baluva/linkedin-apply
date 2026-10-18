@echo off
chcp 65001 >nul
title Candidatures LinkedIn
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python apply.py
echo.
pause
