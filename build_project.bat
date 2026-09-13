@echo off
setlocal EnableExtensions

rem Always build from the directory containing this file.
cd /d "%~dp0"

echo ========================================
echo Akashic Agent - build project
echo ========================================
echo Project directory: %CD%
echo.

where node >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js was not found in PATH.
    echo Install Node.js 18 or newer, then run this file again.
    goto :failed
)

where npm >nul 2>nul
if errorlevel 1 (
    echo [ERROR] npm was not found in PATH.
    echo Install Node.js with npm, then run this file again.
    goto :failed
)

for /f "delims=" %%V in ('node --version') do set "NODE_VERSION=%%V"
for /f "delims=" %%V in ('npm --version') do set "NPM_VERSION=%%V"
echo Node: %NODE_VERSION%
echo npm:  %NPM_VERSION%
echo.

if not exist "package.json" (
    echo [ERROR] package.json was not found. This script must stay in the project root.
    goto :failed
)

if not exist "node_modules" (
    echo [INFO] node_modules is missing. Installing dependencies...
    call npm install
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        goto :failed
    )
    echo.
)

echo [INFO] Running the production build...
call npm run build
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. Review the output above.
    goto :failed
)

echo.
echo [SUCCESS] Build completed.
echo Output directories:
echo   static\dashboard
echo   static\chat
echo   static\plugins
goto :done

:failed
echo.
echo Build was not completed.
pause
exit /b 1

:done
pause
exit /b 0
