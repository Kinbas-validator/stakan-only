@echo off
rem Stakan-only: double-click to install (first run) and start.
rem First run downloads portable Python into the "python" folder next to this file;
rem nothing is installed into Windows and no admin rights are needed.
title Stakan - Lighter RH premium book
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\run.ps1" %*
if errorlevel 1 pause
