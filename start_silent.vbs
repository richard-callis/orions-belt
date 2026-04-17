' Orion's Belt — Silent launcher (no console window)
' The desktop shortcut points here via wscript.exe
Dim projectDir
projectDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
CreateObject("WScript.Shell").Run "cmd /c """ & projectDir & "run.bat""", 0, False
