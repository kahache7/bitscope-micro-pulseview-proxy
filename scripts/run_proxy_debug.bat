@echo off
setlocal

cd /d "%~dp0..\src"
"..\private-package\python32\python.exe" bitscope_rigol_proxy.py

pause
