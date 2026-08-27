@echo off
title OpenGOAL Replay Server
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-replay-server.ps1"
if errorlevel 1 (
  echo.
  echo Replay server setup could not finish. Read the message above, then try again.
  pause
)
