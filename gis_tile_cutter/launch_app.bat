@echo off
cd /d "C:\Users\Bionic Computer\OneDrive\Desktop\gis_tile_cutter"
python main.py > app_launch.log 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: App exited with code %ERRORLEVEL% >> app_launch.log
)
