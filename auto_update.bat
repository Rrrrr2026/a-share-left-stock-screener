@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "data" mkdir "data"
set "LOG=%~dp0data\update.log"
set "PYEXE=C:\Users\roger.DESKTOP-7Q2P0JS\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PYEXE%" set "PYEXE=py"

rem ---- price-library update step + readiness gate (card PC-UPDATE, 2026-09-23) ---------------
rem Ported from stock-core/server/run_a.sh.v2 (the server's 10:00 CEST run). Stage A reads
rem data\pricestore.db directly, so a library that is not updated means every price is stale
rem while the snapshot still carries today's file name (the 09-09..09-23 mirror snapshots were
rem all 09-07 closes: the PC never ran "pricestore update"). Sequence per round:
rem   update  (Tushare, by trade_date; a failure only warns, "ready" decides)
rem   ready TARGET_DAY  (rc 0 = library holds the target day, or no open day up to it;
rem                      rc 1 = not yet; rc 2 = cannot tell, e.g. calendar down. 1 and 2 both wait)
rem   not ready: sleep PSTORE_WAIT_SLEEP seconds, retry, at most PSTORE_WAIT_TRIES rounds.
rem   timeout: write one ABORT line to data\update.log and exit 1. The pipeline is NOT run and
rem            nothing is pushed. Better no mirror update today than yesterday's prices as today's.
rem TARGET_DAY = local calendar date (same ruler as ready_for() and update, both use
rem datetime.date.today()). The task fires 13:30 CEST = 19:30 Beijing (20:30 in winter), hours
rem after Tushare's 15~17 Beijing daily publish, so on a trading day the target is today; on a
rem weekend or holiday "ready" answers rc 0 by itself (no open day between library end and target).
rem Python runs with -X utf8 so its Chinese log lines land in data\update.log as UTF-8 like the
rem pipeline's (a redirected stdout would otherwise be GBK on this PC).
rem Env overrides, for testing only: PSTORE_WAIT_TRIES, PSTORE_WAIT_SLEEP, PSTORE_TARGET_DAY.
rem "auto_update.bat gate" runs only step 0 and the gate, then exits with the gate result.
if not defined PSTORE_WAIT_TRIES set "PSTORE_WAIT_TRIES=6"
if not defined PSTORE_WAIT_SLEEP set "PSTORE_WAIT_SLEEP=600"
set "TARGET_DAY="
for /f "delims=" %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "TARGET_DAY=%%d"
if defined PSTORE_TARGET_DAY set "TARGET_DAY=%PSTORE_TARGET_DAY%"

echo ==================================================
echo   A-share screener: update and publish
echo ==================================================
echo ==== START ==== >> "%LOG%"

echo [0/4] Seed history from published docs (fresh-clone safety) ...
robocopy "docs\history" "dashboard\history" /E /XC /XN /XO /NJH /NJS /NDL /NFL >nul 2>&1
if exist "..\stock-core\leftside_core" robocopy "..\stock-core\leftside_core" "vendor\leftside_core" /MIR /XD __pycache__ /NJH /NJS /NDL /NFL >nul 2>&1

echo [1/4] Update price library and wait until it holds %TARGET_DAY% (up to %PSTORE_WAIT_TRIES% x %PSTORE_WAIT_SLEEP%s) ...
set /a PSTORE_TRIES=0
:pstore_wait
"%PYEXE%" -X utf8 -m ashare.pricestore update >> "%LOG%" 2>&1
if errorlevel 1 echo [pricestore] update exited with %errorlevel% (not caught up this round; "ready" decides) >> "%LOG%"
"%PYEXE%" -X utf8 -m ashare.pricestore ready %TARGET_DAY% >> "%LOG%" 2>&1
set "PSTORE_RC=%errorlevel%"
if "%PSTORE_RC%"=="0" goto :pstore_ok
set /a PSTORE_TRIES+=1
if %PSTORE_TRIES% geq %PSTORE_WAIT_TRIES% goto :pstore_timeout
echo [pricestore] not ready for %TARGET_DAY% (ready rc=%PSTORE_RC%, round %PSTORE_TRIES%/%PSTORE_WAIT_TRIES%), sleeping %PSTORE_WAIT_SLEEP%s >> "%LOG%"
echo   not ready yet (ready rc=%PSTORE_RC%, round %PSTORE_TRIES%/%PSTORE_WAIT_TRIES%), sleeping %PSTORE_WAIT_SLEEP%s ...
powershell -NoProfile -Command "Start-Sleep -Seconds %PSTORE_WAIT_SLEEP%" >nul 2>&1
goto :pstore_wait

:pstore_timeout
echo ==== ABORT ==== price library not ready for %TARGET_DAY% after %PSTORE_WAIT_TRIES% x %PSTORE_WAIT_SLEEP%s (last ready rc=%PSTORE_RC%); pipeline not run, nothing pushed >> "%LOG%"
echo.
echo ABORT: price library not ready for %TARGET_DAY% (ready rc=%PSTORE_RC%). Pipeline not run, nothing pushed. See data\update.log
if /I not "%~1"=="auto" if /I not "%~1"=="gate" pause
exit /b 1

:pstore_ok
echo [pricestore] ready for %TARGET_DAY% (rc=0), continuing >> "%LOG%"
if /I "%~1"=="gate" goto :gate_only

echo [2/4] Fetch data and score (about 10-15 min) ...
"%PYEXE%" watchdog.py >> "%LOG%" 2>&1

echo [3/4] Copy result to docs ...
copy /Y "dashboard\index.html" "docs\index.html" >nul
copy /Y "dashboard\dashboard_data.js" "docs\dashboard_data.js" >nul
if exist "dashboard\backtest_data.js" copy /Y "dashboard\backtest_data.js" "docs\backtest_data.js" >nul
if exist "dashboard\quality_data.js" copy /Y "dashboard\quality_data.js" "docs\quality_data.js" >nul
if exist "dashboard\paper_data.js" copy /Y "dashboard\paper_data.js" "docs\paper_data.js" >nul
if exist "dashboard\biweekly_data.js" copy /Y "dashboard\biweekly_data.js" "docs\biweekly_data.js" >nul
if exist "dashboard\watch_data.js" copy /Y "dashboard\watch_data.js" "docs\watch_data.js" >nul
if exist "dashboard\starmap_data.js" copy /Y "dashboard\starmap_data.js" "docs\starmap_data.js" >nul
if exist "..\stock-core\dashboard\leftside_shared.js" copy /Y "..\stock-core\dashboard\leftside_shared.js" "dashboard\leftside_shared.js" >nul
if exist "dashboard\leftside_shared.js" copy /Y "dashboard\leftside_shared.js" "docs\leftside_shared.js" >nul
robocopy "dashboard\history" "docs\history" /MIR /NJH /NJS /NDL /NFL >nul 2>&1

echo [4/4] Publish to GitHub Pages ...
git add docs vendor >> "%LOG%" 2>&1
git commit -m "auto update data" >> "%LOG%" 2>&1
git pull --rebase --autostash origin main >> "%LOG%" 2>&1
git push >> "%LOG%" 2>&1

echo ==== DONE ==== >> "%LOG%"
echo.
echo Done. Wait 1-2 minutes, then open the "A-share dashboard" desktop shortcut.
echo (log file: data\update.log)
if /I "%~1"=="auto" goto :eof
echo.
pause
goto :eof

:gate_only
echo ==== GATE-ONLY DONE ==== library ready for %TARGET_DAY% >> "%LOG%"
echo Gate only: price library ready for %TARGET_DAY% (rc=0). Pipeline not run, nothing pushed.
exit /b 0
