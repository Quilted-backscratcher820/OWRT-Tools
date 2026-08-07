@echo off
chcp 65001 >nul 2>nul
setlocal

if not defined CORE_TOOL_ROOT goto invalid_config
if not defined CORE_TOOL_WSL_SCRIPT goto invalid_config
set "SCRIPT_DIR=%CORE_TOOL_ROOT%"

if /i "%SCRIPT_DIR:~0,16%"=="\\wsl.localhost\" goto wsl_localhost
if /i "%SCRIPT_DIR:~0,7%"=="\\wsl$\" goto wsl_dollar
goto windows_unsupported

:wsl_localhost
set "RELATIVE_PATH=%SCRIPT_DIR:~16%"
goto launch_wsl

:wsl_dollar
set "RELATIVE_PATH=%SCRIPT_DIR:~7%"

:launch_wsl
for /f "tokens=1,* delims=\" %%A in ("%RELATIVE_PATH%") do (
    if not defined CORE_TOOL_WSL_DISTRO set "DISTRO=%%A"
    set "LINUX_PATH=%%B"
)
if defined CORE_TOOL_WSL_DISTRO set "DISTRO=%CORE_TOOL_WSL_DISTRO%"
if not defined DISTRO goto invalid_config
if not defined LINUX_PATH set "LINUX_PATH="
set "LINUX_PATH=/%LINUX_PATH:\=/%"
if not exist "%SystemRoot%\System32\wsl.exe" goto wsl_unavailable

%SystemRoot%\System32\wsl.exe -d %DISTRO% --cd / -- sh -c "exit 0" >nul 2>nul
if not "%ERRORLEVEL%"=="0" goto launch_default_wsl

:launch_selected_wsl
%SystemRoot%\System32\wsl.exe -d %DISTRO% --cd "%LINUX_PATH%" -- sh "./%CORE_TOOL_WSL_SCRIPT%" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" goto launch_failed
if defined CORE_TOOL_PAUSE_ON_SUCCESS goto completed
exit /b 0

:launch_default_wsl
%SystemRoot%\System32\wsl.exe --cd "%LINUX_PATH%" -- sh "./%CORE_TOOL_WSL_SCRIPT%" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" goto launch_failed
if defined CORE_TOOL_PAUSE_ON_SUCCESS goto completed
exit /b 0

:windows_unsupported
echo.
echo [OpenWrt] 不支持 Windows 原生路径，请从 WSL2 UNC 路径启动。
echo.
if not defined CORE_TOOL_NO_PAUSE pause
exit /b 2

:wsl_unavailable
echo [OpenWrt] 未检测到 WSL2，请先安装或启用。
goto pause_error

:launch_failed
echo [OpenWrt] 在 WSL 中启动失败，退出代码 %RESULT%。
goto pause_result

:completed
echo.
if not defined CORE_TOOL_NO_PAUSE pause
exit /b 0

:invalid_config
set "RESULT=2"
echo [OpenWrt] WSL 启动配置无效。
goto pause_result

:pause_error
set "RESULT=1"

:pause_result
echo.
if not defined CORE_TOOL_NO_PAUSE pause
exit /b %RESULT%
