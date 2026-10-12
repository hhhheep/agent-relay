Option Explicit
Dim shell, fso, here, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
' Resolve an installed home explicitly, or run beside this source file.
' A copied Startup entry requires RESEARCH_BACKGROUND_HOME to be configured.
here = shell.ExpandEnvironmentStrings("%RESEARCH_BACKGROUND_HOME%")
If here = "%RESEARCH_BACKGROUND_HOME%" Then
  here = fso.GetParentFolderName(WScript.ScriptFullName)
End If
If Not fso.FileExists(here & "\NoConsoleLauncher.exe") Then WScript.Quit 1
command = """" & here & "\NoConsoleLauncher.exe"""
shell.Run command, 0, False
