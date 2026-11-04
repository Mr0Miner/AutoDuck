@echo off
REM Build AutoDuck into dist\AutoDuck\AutoDuck.exe
setlocal

echo [1/2] Installing dependencies...
python -m pip install -r requirements.txt || goto :err

echo [2/2] Building with PyInstaller...
python -m PyInstaller AutoDuck.spec --noconfirm || goto :err

echo.
echo Done. Run: dist\AutoDuck\AutoDuck.exe
exit /b 0

:err
echo.
echo Build failed.
exit /b 1
