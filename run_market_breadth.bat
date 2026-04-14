@echo off
title Vietnam Market Breadth - Dang chay...
cd /d "%~dp0"
echo.
echo ========================================
echo  Vietnam Market Breadth - Co hoi Chart
echo ========================================
echo.
python market_breadth.py
echo.
if errorlevel 1 (
    echo [LOI] Script gap loi. Xem thong bao phia tren.
    pause
) else (
    echo [OK] Hoan thanh! Trinh duyet se tu mo.
    timeout /t 3 >nul
)
