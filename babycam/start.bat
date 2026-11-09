@echo off
chcp 65001 >nul
cd /d "%~dp0"
title BabyCam Server
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% server.py
if errorlevel 1 (
  echo.
  echo 파이썬을 찾을 수 없거나 서버 실행에 실패했습니다.
  echo https://www.python.org/downloads/ 에서 설치하세요 ^(설치 시 "Add python.exe to PATH" 체크^).
)
pause
