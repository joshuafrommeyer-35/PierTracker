@echo off
rem Sets up LiveCams + PierTracker on this PC and starts the cam wallpapers, the tracker and the
rem tray icon. After a Windows reset it also installs what's missing and restores your data from
rem the Google Drive backup. Safe to run any time: finished steps are skipped.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
echo.
pause
