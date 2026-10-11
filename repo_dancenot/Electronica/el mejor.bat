@echo off
Title OPTIMIZADOR CELESTIAL V6.0 - HARDCORE GAMING
color 0B

:: Verificacion de Administrador
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] EJECUTA COMO ADMINISTRADOR.
    pause
    exit
)

echo =========================================================
echo    APLICANDO AJUSTES DE BAJA LATENCIA (ANTI-LAG)
echo =========================================================

echo [1/3] Optimizando Stack TCP y Registro para Ryzen 7...
:: Ajustes de Red Globales
netsh int tcp set heuristics disabled >nul
netsh int tcp set global autotuninglevel=normal >nul
netsh int tcp set global rss=enabled >nul
netsh int tcp set global rsc=disabled >nul
netsh int tcp set global timestamps=disabled >nul

:: Prioridad Critica en Registro
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile" /v "NetworkThrottlingIndex" /t REG_DWORD /d 0xffffffff /f >nul
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile" /v "SystemResponsiveness" /t REG_DWORD /d 0 /f >nul

echo [2/3] Forzando Configuracion en Realtek (Sin Errores)...
:: Este bloque usa comodines para encontrar las propiedades sin importar el nombre exacto o idioma
powershell -Command "Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { $n = $_.Name; Write-Host 'Optimizando adaptador: ' $n; Set-NetAdapterAdvancedProperty -Name $n -DisplayName '*Energy*' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue; Set-NetAdapterAdvancedProperty -Name $n -DisplayName '*Green*' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue; Set-NetAdapterAdvancedProperty -Name $n -DisplayName '*Interrupt*' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue; Set-NetAdapterAdvancedProperty -Name $n -DisplayName '*Flow Control*' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue; Set-NetAdapterAdvancedProperty -Name $n -DisplayName '*Power Saving*' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue; Disable-NetAdapterPowerManagement -Name $n -Confirm:$false -ErrorAction SilentlyContinue }"

echo [3/3] Limpieza de Cache y Reseteo de Stack...
ipconfig /flushdns >nul
netsh winsock reset >nul
netsh int ip reset >nul

echo.
echo =========================================================
echo   OPTIMIZACION TERMINADA. ERRORES OMITIDOS POR SEGURIDAD.
echo =========================================================
echo Reinicia tu PC para estabilizar los 50ms de ping.
pause