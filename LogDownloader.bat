@echo off
chcp 65001 >nul
title Log Downloader
cd /d "%~dp0"
if not exist "runtime\python.exe" goto missing
echo.
echo   로그 다운로더를 시작합니다.
echo   브라우저가 자동으로 열립니다. 사용 중에는 이 창을 열어 두세요.
echo   주소: http://127.0.0.1:8778/
echo.
"runtime\python.exe" -E -s "app\server.py"
if errorlevel 1 pause
exit /b
:missing
echo   ZIP 파일을 먼저 모두 압축 해제해 주세요.
echo   runtime 폴더가 없으면 GitHub에서 Windows용 ZIP을 다시 받으세요.
pause
