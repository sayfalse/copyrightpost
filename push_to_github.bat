@echo off
:: ============================================================
::  push_to_github.bat
::  Pushes the Manager Bot files to:
::  https://github.com/sayfalse/copyrightpost
::
::  Run this ONCE from the folder that contains your bot files.
::  Requirements: git must be installed and on PATH.
::  https://git-scm.com/download/win
:: ============================================================

setlocal

:: ── CONFIG — change these if needed ────────────────────────
set REPO_URL=https://github.com/sayfalse/copyrightpost.git
set BRANCH=main
:: ────────────────────────────────────────────────────────────

echo.
echo ============================================================
echo  Manager Bot — GitHub Push Script
echo ============================================================
echo.

:: Check git is available
where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] git is not installed or not on PATH.
    echo         Download from: https://git-scm.com/download/win
    pause
    exit /b 1
)

:: Initialise repo if not already done
if not exist ".git" (
    echo [INIT] Initialising git repository...
    git init
    if errorlevel 1 ( echo [ERROR] git init failed. & pause & exit /b 1 )
    git remote add origin %REPO_URL%
) else (
    echo [INFO] Git repository already initialised.
    :: Make sure remote is set correctly
    git remote set-url origin %REPO_URL%
)

:: Stage all files
echo [ADD] Staging all files...
git add .
if errorlevel 1 ( echo [ERROR] git add failed. & pause & exit /b 1 )

:: Commit
echo [COMMIT] Committing...
git commit -m "deploy: production-ready manager bot with Telegram storage"
if errorlevel 1 (
    echo [INFO] Nothing new to commit, or commit failed. Trying to push anyway.
)

:: Set branch to main and push
echo [PUSH] Pushing to %REPO_URL% on branch %BRANCH%...
git branch -M %BRANCH%
git push -u origin %BRANCH%

if errorlevel 1 (
    echo.
    echo [ERROR] Push failed.
    echo         If this is your first push you may need to authenticate.
    echo         GitHub no longer accepts passwords — use a Personal Access Token:
    echo           1. Go to: https://github.com/settings/tokens
    echo           2. Generate new token (classic) with 'repo' scope
    echo           3. When git asks for your password, paste the token instead.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  SUCCESS! Files pushed to:
echo  https://github.com/sayfalse/copyrightpost
echo ============================================================
echo.
pause
endlocal
