Option Explicit

Dim shell
Dim fso
Dim repoRoot
Dim scriptPath
Dim configuratorRoot
Dim exePath
Dim command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

repoRoot = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = repoRoot & "\scripts\Start-Phase1Configurator.ps1"
configuratorRoot = repoRoot & "\desktop-configurator"
exePath = configuratorRoot & "\src-tauri\target\release\phase1-desktop-configurator.exe"

shell.CurrentDirectory = repoRoot

If Not fso.FileExists(scriptPath) Then
  MsgBox "Could not find the configurator launch script:" & vbCrLf & scriptPath, vbCritical, "Phase1 Configurator"
  WScript.Quit 1
End If

If fso.FileExists(exePath) Then
  command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command ""Start-Process -FilePath '" & Replace(exePath, "'", "''") & "' -WorkingDirectory '" & Replace(configuratorRoot, "'", "''") & "'"""
  shell.Run command, 0, False
Else
  shell.Popup "First launch needs to build the desktop configurator locally. This can take 1 to 5 minutes. A PowerShell window will open during the build.", 8, "Phase1 Configurator", 64
  command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & scriptPath & """"
  shell.Run command, 1, False
End If
