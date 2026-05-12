@echo off
setlocal

set "ROOT=%~dp0.."

start "" /D "%ROOT%\src" "%ROOT%\private-package\python32\pythonw.exe" "bitscope_rigol_proxy.py"

exit /b