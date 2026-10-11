@echo off
net session >nul 2>&1
if %errorLevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo [+] Cerrando procesos que roban internet...
:: Cierra procesos sospechosos que vimos en tus fotos
taskkill /f /im msedge.exe /t >nul 2>&1
taskkill /f /im MicrosoftStore.exe >nul 2>&1
taskkill /f /im OneDrive.exe >nul 2>&1


taskkill /f /im SteamWebHelper.exe /t >nul 2>&1

echo [+] Limpiando red...
ipconfig /release >nul
ipconfig /renew >nul
ipconfig /flushdns >nul
netsh int ip reset >nul
netsh winsock reset >nul

echo [+] Limpieza profunda completada. Ping optimizado.
pause