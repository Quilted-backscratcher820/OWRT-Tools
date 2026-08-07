@echo off
chcp 65001 >nul 2>nul
setlocal
set "CORE_TOOL_ROOT=%~dp0"
set "CORE_TOOL_WSL_SCRIPT=run_owrt_linux.sh"
call "%~dp0support\wsl_bridge.cmd" %*
exit /b %ERRORLEVEL%
