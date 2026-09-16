@echo off
REM Sign this PC in to GitHub, once, so push-to-github.bat can push.
REM
REM Run this FIRST on any PC that hasn't pushed Hanwha Monitor before.
REM One-time per machine. Safe to run again: if the PC is already signed
REM in it says so and changes nothing unless you ask it to.

setlocal
cd /d "%~dp0"
title Hanwha Monitor - GitHub sign-in

echo ============================================
echo   Hanwha Monitor - one-time GitHub sign-in
echo ============================================
echo.

REM ---- git ---------------------------------------------------------
where git >nul 2>nul
if not errorlevel 1 goto git_ok
echo Git isn't installed, or isn't on your PATH.
echo.
echo Installing it with winget...
echo.
winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
echo.
echo Git was installed, but this window still has the OLD PATH and
echo cannot see it. That is normal and is not an error.
echo.
echo   Close this window and run this file once more.
echo.
pause
exit /b 0
:git_ok
echo Git: found.

REM ---- gh (GitHub CLI holds the sign-in - no token to paste anywhere) -
where gh >nul 2>nul
if not errorlevel 1 goto gh_ok
echo GitHub CLI isn't installed. Installing it with winget...
echo.
winget install --id GitHub.cli -e --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto gh_install_failed
echo.
echo GitHub CLI was installed, but this window still has the OLD PATH
echo and cannot see it yet. That is normal and is not an error.
echo.
echo   Close this window and run this file once more.
echo.
pause
exit /b 0
:gh_install_failed
echo.
echo winget couldn't install it. Download it by hand from:
echo   https://cli.github.com
echo ...then run this file again.
echo.
pause
exit /b 1
:gh_ok
echo GitHub CLI: found.
echo.

REM ---- already signed in? -----------------------------------------
gh auth status >nul 2>nul
if errorlevel 1 goto sign_in
echo This PC is already signed in to GitHub:
echo.
gh auth status
echo.
echo Nothing more is needed - run push-to-github.bat.
echo.
choice /c YN /n /m "Sign in again as a different account anyway? [Y/N] "
if errorlevel 2 goto configure_git
echo.

REM ---- sign in -----------------------------------------------------
:sign_in
echo Signing in to GitHub.
echo.
echo   A browser will open and ask for a one-time code.
echo   The code is printed here, just above the prompt - copy it.
echo   Sign in as the account the hanwha-monitor repository should live under.
echo.
pause
echo.
gh auth login --hostname github.com --git-protocol https --web
if errorlevel 1 goto login_failed
goto configure_git

:login_failed
echo.
echo Sign-in didn't complete. The message above says why.
echo.
echo If the browser never opened, run this instead and paste the URL
echo into a browser by hand:
echo   gh auth login --hostname github.com --git-protocol https
echo.
pause
exit /b 1

:configure_git
echo.
REM setup-git makes plain `git push` use the same sign-in.
echo Letting git use that sign-in...
gh auth setup-git
if errorlevel 1 goto setup_git_failed

REM A name and email on commits (flat labels, not nested blocks - %VAR%
REM inside a ( ) block is expanded before the block runs).
for /f "tokens=*" %%A in ('git config --global user.name 2^>nul') do set HAVENAME=%%A
if defined HAVENAME goto identity_done
for /f "tokens=*" %%A in ('gh api user --jq .login 2^>nul') do set GHUSER=%%A
if not defined GHUSER goto identity_done
git config --global user.name "%GHUSER%"
git config --global user.email "%GHUSER%@users.noreply.github.com"
echo Set your git commit name to %GHUSER%.
:identity_done

echo.
echo ============================================
echo   Done. This PC is signed in.
echo ============================================
echo.
gh auth status
echo.
echo You only ever need to do this once on this PC.
echo Now run push-to-github.bat.
echo.
pause
exit /b 0

:setup_git_failed
echo.
echo Signed in, but git couldn't be pointed at it. Try running:
echo   gh auth setup-git
echo.
pause
exit /b 1
