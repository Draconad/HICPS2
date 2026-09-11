@echo off
REM Push this folder to GitHub, start the cloud builds, and download the
REM finished iPhone .ipa and Windows .exe into build-out\
REM (the iPhone app is only built when you say yes - it uses the most build minutes)
REM
REM Double-click it. First time on a PC? Run github-auth.bat first.
REM
REM The repository is <your GitHub username>/hanwha-monitor (private). It is
REM created automatically the first time. To use a different one, put
REM "owner/name" on the first line of github-repo.txt next to this file.
REM
REM Safe to run more than once. The window always stays open at the end.

setlocal
cd /d "%~dp0"
title Hanwha Monitor - push to GitHub

where git >nul 2>nul
if errorlevel 1 goto no_tools
where gh >nul 2>nul
if errorlevel 1 goto no_tools
gh auth status >nul 2>nul
if errorlevel 1 goto not_signed_in

REM ---- which repository ------------------------------------------------
set "REPO="
if exist "github-repo.txt" set /p REPO=<github-repo.txt
if defined REPO goto have_repo
for /f "tokens=*" %%A in ('gh api user --jq .login 2^>nul') do set GHUSER=%%A
if not defined GHUSER goto not_signed_in
set "REPO=%GHUSER%/hanwha-monitor"
:have_repo
echo Repository: %REPO%

REM ---- create it on GitHub the first time --------------------------------
gh repo view "%REPO%" >nul 2>nul
if not errorlevel 1 goto repo_exists
echo Creating private repository %REPO% on GitHub...
gh repo create "%REPO%" --private --description "Hanwha XE35 lathe monitor"
if errorlevel 1 goto failed
:repo_exists
>github-repo.txt echo %REPO%

REM ---- local git ----------------------------------------------------------
if exist ".git" goto have_git
echo Setting up git in this folder...
git init -q
if errorlevel 1 goto failed
:have_git
git branch -M main >nul 2>nul

REM Commits need a name; github-auth.bat normally sets one.
git config user.name >nul 2>nul
if not errorlevel 1 goto have_identity
for /f "tokens=*" %%A in ('gh api user --jq .login 2^>nul') do set GHUSER=%%A
git config user.name "%GHUSER%"
git config user.email "%GHUSER%@users.noreply.github.com"
:have_identity

REM Commit anything that changed (new files from a zip, or your own edits).
git add -A
git diff --cached --quiet
if not errorlevel 1 goto nothing_new
echo Saving changes...
git commit -q -m "Update from %COMPUTERNAME% %DATE% %TIME%"
if errorlevel 1 goto failed
goto pushing
:nothing_new
echo No local changes - pushing what's already committed.

:pushing
gh auth setup-git >nul 2>nul
git remote remove origin >nul 2>nul
git remote add origin https://github.com/%REPO%.git
if errorlevel 1 goto failed

echo.
echo Pushing...
git push --force -u origin main
if errorlevel 1 goto push_failed

echo.
echo Pushed.

REM ---- iPhone app: only built when asked (macOS build minutes count 10x) ----
set "LAST_IOS="
for /f "tokens=*" %%A in ('gh run list --repo %REPO% --workflow ios.yml --status success --limit 1 --json headSha --jq ".[0].headSha" 2^>nul') do set "LAST_IOS=%%A"
set "IOS_CHANGED=1"
if not defined LAST_IOS goto ios_decided
git diff --quiet %LAST_IOS% HEAD -- ios >nul 2>nul
if not errorlevel 1 set "IOS_CHANGED=0"
:ios_decided
if "%IOS_CHANGED%"=="0" goto after_ios
echo.
echo The iPhone app has changed since its last build.
echo An iPhone build uses about 100 of GitHub's 2,000 free build minutes a month,
echo so only build it when you want to install the new version.
choice /c YN /t 20 /d N /m "Build the iPhone app now (No in 20 s)"
if errorlevel 2 goto after_ios
gh workflow run ios.yml --repo %REPO% --ref main
if errorlevel 1 echo Couldn't start the iPhone build - start it from the Actions tab on GitHub.
:after_ios

echo.
echo Now waiting for the builds...
echo.

REM The waiting/downloading half is PowerShell (it has to read JSON). If it
REM breaks, the code is still safely on GitHub and the builds still run.
if not exist "%~dp0wait-for-builds.ps1" goto no_waiter
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wait-for-builds.ps1" -Repo "%REPO%"
echo.
pause
exit /b %errorlevel%

:no_waiter
echo Done. The builds start on their own - see https://github.com/%REPO%/actions
echo.
pause
exit /b 0

:no_tools
echo Git or GitHub CLI isn't installed on this PC yet.
echo Run github-auth.bat first - it installs both and signs you in.
echo.
pause
exit /b 1

:not_signed_in
echo This PC isn't signed in to GitHub yet.
echo Run github-auth.bat first, then run this again.
echo.
pause
exit /b 1

:push_failed
echo.
echo The push didn't work. The error above says why.
echo.
echo If it mentions authentication, credentials, or a 403, run
echo github-auth.bat once, then run this again.
echo.
pause
exit /b 1

:failed
echo.
echo That didn't work. The error above says why.
echo.
pause
exit /b 1
