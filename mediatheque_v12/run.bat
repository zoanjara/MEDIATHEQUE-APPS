@echo off
title Mediatheque locale
cd /d "%~dp0"

rem ===========================================================
rem  Script unique : installe automatiquement au premier lancement
rem  (creation du venv + dependances), puis demarre a chaque fois.
rem  Aucun autre script a executer avant celui-ci.
rem ===========================================================

if exist "venv\Scripts\python.exe" goto :start

where python >nul 2>nul
if errorlevel 1 (
    echo [ERREUR] Python n'est pas trouve dans le PATH.
    echo Installe Python 3.10+ depuis https://www.python.org/downloads/
    echo IMPORTANT: coche "Add python.exe to PATH" lors de l'installation.
    pause
    exit /b 1
)

echo ============================================
echo   Premier lancement : installation en cours
echo   (necessite Internet une seule fois)
echo ============================================
echo.
echo Creation de l'environnement virtuel (venv)...
python -m venv venv
if not exist "venv\Scripts\python.exe" (
    echo [ERREUR] La creation de l'environnement virtuel a echoue.
    pause
    exit /b 1
)

echo Installation des dependances...
call venv\Scripts\pip.exe install --upgrade pip
call venv\Scripts\pip.exe install -r requirements.txt
if errorlevel 1 (
    echo [ERREUR] L'installation des dependances a echoue.
    echo Verifie ta connexion Internet puis relance ce script.
    pause
    exit /b 1
)

echo.
echo Installation terminee !
echo.

:start
echo Demarrage de la Mediatheque locale...
echo Le navigateur va s'ouvrir automatiquement sur http://127.0.0.1:5000
echo Pour arreter le serveur : ferme cette fenetre ou appuie sur Ctrl+C.
echo.

venv\Scripts\python.exe app.py

pause
