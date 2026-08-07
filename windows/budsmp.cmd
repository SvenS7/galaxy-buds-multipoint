@echo off
rem budsmp - Galaxy Buds multipoint enabler (Windows)
rem
rem A thin wrapper so you can run budsmp from this directory. It is equivalent to
rem     set PYTHONPATH=..\python
rem     py -3 -m budsmp %*
rem and exists only to save the typing; there is nothing to build or install.

setlocal
set "PYTHONPATH=%~dp0..\python;%PYTHONPATH%"

where /q py.exe
if %ERRORLEVEL%==0 (
    py -3 -m budsmp %*
    exit /b %ERRORLEVEL%
)

where /q python.exe
if %ERRORLEVEL%==0 (
    python -m budsmp %*
    exit /b %ERRORLEVEL%
)

echo budsmp: no Python found in PATH ^(Python 3.9 or newer is required^)>&2
exit /b 1
