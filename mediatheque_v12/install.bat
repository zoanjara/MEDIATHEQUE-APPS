@echo off
title Mediatheque - Reinstallation
cd /d "%~dp0"

rem ===========================================================
rem  Ce script n'est PAS necessaire pour une premiere utilisation :
rem  run.bat installe desormais tout automatiquement tout seul au
rem  premier lancement. Ce script sert uniquement a FORCER une
rem  reinstallation complete et propre (environnement virtuel
rem  supprime puis recree), par exemple si l'environnement est
rem  corrompu ou apres un probleme de dependances.
rem ===========================================================

echo ============================================
echo   Reinstallation complete de la Mediatheque
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERREUR] Python n'est pas trouve dans le PATH.
    echo Installe Python 3.10+ depuis https://www.python.org/downloads/
    echo IMPORTANT: coche "Add python.exe to PATH" lors de l'installation.
    pause
    exit /b 1
)

if exist "venv" (
    echo Suppression de l'ancien environnement virtuel...
    rmdir /s /q venv
)

echo Creation de l'environnement virtuel (venv)...
python -m venv venv
if not exist "venv\Scripts\python.exe" (
    echo [ERREUR] La creation de l'environnement virtuel a echoue.
    pause
    exit /b 1
)

echo Installation des dependances (necessite Internet une seule fois)...
call venv\Scripts\pip.exe install --upgrade pip
call venv\Scripts\pip.exe install -r requirements.txt
if errorlevel 1 (
    echo [ERREUR] L'installation des dependances a echoue.
    echo Verifie ta connexion Internet puis relance ce script.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Reinstallation terminee !
echo   Lance maintenant run.bat pour demarrer.
echo ============================================
pause
