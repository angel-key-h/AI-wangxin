@echo off
rem ===========================================================================
rem  Multimodal Explainable QA Agent - one-click launcher (Windows)
rem  Task 8 / China Electronics Cloud - ICT Competition
rem
rem  Usage:
rem     start.bat               check env -> install deps -> init -> launch
rem     start.bat --init-only   prepare everything, do NOT launch Streamlit
rem     start.bat --reinit      force rebuild the SQL database, then launch
rem     start.bat --schema      only re-export data\schema.sql
rem     start.bat --help
rem
rem  This file is intentionally ASCII-only. cmd.exe reads batch files with the
rem  OEM code page (GBK on zh-CN Windows); non-ASCII literals here would be
rem  garbled. All Chinese notes live in README.md instead.
rem ===========================================================================
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

echo ===========================================================================
echo  Multimodal Explainable QA Agent  -  launch
echo ===========================================================================
echo.

set "MODE=run"
if /i "%~1"=="--help"      goto :usage
if /i "%~1"=="-h"          goto :usage
if /i "%~1"=="--init-only" set "MODE=init"
if /i "%~1"=="--reinit"    set "MODE=reinit"
if /i "%~1"=="--schema"    set "MODE=schema"

rem ---------------------------------------------------------------------------
rem  1. pick a Python interpreter
rem     Do not blindly trust "python": other tools on PATH may ship their own
rem     isolated interpreter that lacks our dependencies. So we prefer the first
rem     candidate that ALREADY has streamlit, and only fall back to installing.
rem     Override with:  set PYTHON_BIN=C:\path\to\python.exe
rem ---------------------------------------------------------------------------
set "PY="
if defined PYTHON_BIN set "PY=%PYTHON_BIN%"
if defined PY goto :have_py

py -3 -c "import streamlit" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :have_py

python -c "import streamlit" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :have_py

rem no ready interpreter found - pick whatever exists and install into it
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :have_py

python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :have_py

echo [ERROR] No usable Python found.
echo         Install Python 3.10+ from https://www.python.org/downloads/
echo         and tick "Add python.exe to PATH" during setup.
echo         Then run this script again.
goto :fail

:have_py
for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%v"
if not defined PYVER set "PYVER=?"
echo [1/5] Python %PYVER%   [interpreter: %PY%]

rem ---------------------------------------------------------------------------
rem  2. dependencies
rem ---------------------------------------------------------------------------
%PY% -c "import streamlit" >nul 2>nul
if errorlevel 1 goto :install_deps
echo [2/5] Dependencies OK
goto :after_deps

:install_deps
echo [2/5] Installing dependencies - this may take several minutes ...
echo.
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] "pip install" failed. Try manually:
    echo            %PY% -m pip install -r requirements.txt
    goto :fail
)
echo.

:after_deps

rem ---------------------------------------------------------------------------
rem  3. API keys
rem ---------------------------------------------------------------------------
if not exist ".env" goto :no_env
echo [3/5] .env found
goto :after_env

:no_env
echo [3/5] WARNING: .env not found - SQL and RAG will fail with HTTP 401.
echo         Create .env next to this script. See README.md section 5:
echo           LLM_API_KEY=...        LLM_MODEL=qwen3-max
echo           EMBEDDING_API_KEY=...  EMBEDDING_MODEL=text-embedding-v4
echo.

:after_env

rem ---------------------------------------------------------------------------
rem  4. runtime init (idempotent: database + schema definition file)
rem ---------------------------------------------------------------------------
echo [4/5] Preparing runtime data ...
if /i "%MODE%"=="schema" goto :schema_only
if /i "%MODE%"=="reinit" goto :reinit
%PY% scripts\init_runtime.py
if errorlevel 1 echo [WARN] Initialisation reported problems - the app may still start.
goto :after_init

:reinit
%PY% scripts\init_runtime.py --force
if errorlevel 1 echo [WARN] Initialisation reported problems.
goto :after_init

:schema_only
%PY% scripts\init_runtime.py --schema-only
if errorlevel 1 goto :fail
if /i "%MODE%"=="schema" goto :done

:after_init

rem ---------------------------------------------------------------------------
rem  5. launch
rem ---------------------------------------------------------------------------
if /i "%MODE%"=="init" goto :done

echo [5/5] Starting Streamlit - press Ctrl+C to stop
echo.
%PY% -m streamlit run app.py
if errorlevel 1 (
    echo.
    echo [ERROR] Streamlit exited with an error.
    goto :fail
)
goto :done

:usage
echo Usage:
echo    start.bat               check env -^> install deps -^> init -^> launch
echo    start.bat --init-only   prepare everything, do NOT launch Streamlit
echo    start.bat --reinit      force rebuild the SQL database, then launch
echo    start.bat --schema      only re-export data\schema.sql
echo    start.bat --help        show this help
echo.
echo Env override:
echo    set PYTHON_BIN=C:\path\to\python.exe   force a specific interpreter
goto :done

:done
endlocal
exit /b 0

:fail
echo.
pause
endlocal
exit /b 1
