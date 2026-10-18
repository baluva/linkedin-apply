@echo off
chcp 65001 >nul
title Candidatures spontanees
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python spontane.py run
echo.
pause
