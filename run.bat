@echo off
setlocal EnableExtensions
rem Historia - khoi dong Studio tai http://127.0.0.1:PORT (mac dinh 8000).
rem Cach dung:  run.bat          hoac   run.bat 8001
rem Luu y: tat cua so nay chi dung app tren may ban, KHONG dung tien thue Pod RunPod.

cd /d "%~dp0"
title Historia Studio

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%"
set "PY=.venv\Scripts\python.exe"

rem --- 1. Moi truong Python (.venv) ---
if not exist "%PY%" (
  echo Chua co .venv - dang tao moi truong Python...
  where py >nul 2>&1 && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist "%PY%" (
    echo [LOI] Khong tao duoc .venv. Cai Python 3.11+ tu python.org roi chay lai.
    goto :fail
  )
)

rem Cai lai thu vien chi khi pyproject.toml thay doi so voi lan cai truoc.
fc /b "pyproject.toml" ".venv\pyproject.installed" >nul 2>&1
if errorlevel 1 (
  echo Dang cai/cap nhat thu vien Python theo pyproject.toml...
  "%PY%" -m pip install --upgrade pip >nul
  "%PY%" -c "import tomllib,subprocess,sys;d=tomllib.load(open('pyproject.toml','rb'));sys.exit(subprocess.call([sys.executable,'-m','pip','install',*d['project']['dependencies']]))"
  if errorlevel 1 (
    echo [LOI] Cai thu vien that bai. Xem thong bao pip phia tren.
    goto :fail
  )
  copy /y "pyproject.toml" ".venv\pyproject.installed" >nul
)

rem --- 2. Giao dien web: build lai khi ma nguon moi hon ban build (dist khong nam trong git) ---
set "FE=ghm\frontend"
set "NEEDBUILD="
if not exist "%FE%\dist\index.html" set "NEEDBUILD=1"
if not defined NEEDBUILD (
  powershell -NoProfile -Command "$d=(Get-Item '%FE%\dist\index.html').LastWriteTimeUtc; $n=Get-ChildItem '%FE%\src','%FE%\index.html','%FE%\package.json','%FE%\vite.config.ts' -Recurse -File | Where-Object { $_.LastWriteTimeUtc -gt $d } | Select-Object -First 1; if ($n) { exit 1 } else { exit 0 }"
  if errorlevel 1 set "NEEDBUILD=1"
)
if defined NEEDBUILD (
  where npm >nul 2>&1
  if errorlevel 1 (
    echo [LOI] Giao dien can build lai nhung may khong co Node.js/npm. Cai Node.js 20+ roi chay lai.
    goto :fail
  )
  echo Ma nguon giao dien da thay doi - dang build lai...
  pushd "%FE%"
  set "NEEDCI="
  if not exist "node_modules\.package-lock.json" set "NEEDCI=1"
  if exist "node_modules\.package-lock.json" powershell -NoProfile -Command "if ((Get-Item 'package-lock.json').LastWriteTimeUtc -gt (Get-Item 'node_modules\.package-lock.json').LastWriteTimeUtc) { exit 1 }" || set "NEEDCI=1"
  if defined NEEDCI call npm ci
  set "BUILDRC=0"
  call npm run build || set "BUILDRC=1"
  popd
)
if defined NEEDBUILD if not "%BUILDRC%"=="0" (
  echo [LOI] Build giao dien that bai. Xem loi phia tren.
  goto :fail
)

rem --- 3. Neu Historia dang chay o cong nay thi chi mo trinh duyet (giao dien da build moi) ---
netstat -ano | findstr /r /c:"127\.0\.0\.1:%PORT% .*LISTENING" /c:"0\.0\.0\.0:%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo Cong %PORT% dang co ung dung chay. Mo %URL% ...
  echo Neu vua cap nhat code Python, dong cua so Historia cu roi chay lai run.bat.
  start "" "%URL%"
  exit /b 0
)

rem --- 4. Mo trinh duyet khi server san sang (toi da 60 giay) ---
start "" /b powershell -NoProfile -WindowStyle Hidden -Command ^
  "for($i=0;$i -lt 60;$i++){try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',%PORT%);$c.Close();break}catch{Start-Sleep -Seconds 1}};Start-Process '%URL%'"

echo.
echo  Historia Studio: %URL%
echo  Dong cua so nay (hoac Ctrl+C) de dung app. Pod RunPod van tinh tien cho toi khi ban dung Pod.
echo.
"%PY%" -m ghm.cli serve --port %PORT%
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Historia da dung do loi. Nhan phim bat ky de dong.
pause >nul
exit /b 1
