@echo off
rem Thin shim. The real test harness is _probe_test.py.
rem Why the shim exists at all: this file used to BE the test, and it kept
rem its own hand-written copy of the :probe subroutine -- while its own
rem header claimed it "copies that block verbatim". That is a copy-paste
rem time bomb: edit start-cacx.bat, forget to edit this, and the test
rem keeps passing while verifying code that no longer ships.
rem The Python harness extracts the decision block out of start-cacx.bat by
rem anchors, so there is exactly one source of truth and a stale test is
rem impossible. It also has to write its temp .bat as pure ASCII, which is
rem awkward from here.
rem
rem Usage (from a cmd window; the full path has non-ASCII characters, so
rem do NOT put that path inside this file):
rem     py -3 -X utf8 "%~dp0_probe_test.py"
rem ASCII only; cmd.exe decodes .bat with the OEM codepage and a UTF-8
rem Chinese line would be executed as a command.
rem NOTE how the harness is invoked: through %~dp0, NEVER by writing the
rem   containing folder's name out. That folder is Chinese-named, and this
rem   file must stay pure ASCII -- _fix_bat_crlf.py rejects non-ASCII bytes
rem   here for exactly the reason it caught that mistake the first time.
rem   A UTF-8 Chinese path inside a .bat does not merely look wrong; cmd can
rem   chop it and execute the fragment as a command.
setlocal
where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] the py launcher is required to run the harness.
    exit /b 1
)
py -3 -X utf8 "%~dp0_probe_test.py"
exit /b %ERRORLEVEL%
