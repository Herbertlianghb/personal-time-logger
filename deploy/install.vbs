Option Explicit
Dim shell, files, folder, powershell, command, status
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
command = """" & powershell & """ -NoProfile -ExecutionPolicy Bypass -File """ & folder & "\install-desktop.ps1"" -PackagePath """ & folder & """ -Launch"
status = shell.Run(command, 0, True)
If status <> 0 Then
    MsgBox "Installation did not finish. Run install-desktop.ps1 in PowerShell to read the error; see docs/SETUP.md.", vbExclamation, "Personal Time Logger"
End If
