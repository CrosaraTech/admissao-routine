' ============================================================
' AdmitER Web - Inicializacao silenciosa (sem janela)
' ============================================================
' Roda iniciar-web.bat em background, sem mostrar console.
' Ideal pra colocar em shell:startup ou Task Scheduler.
'
' Uso:
'   1. Pressione Win+R, digite shell:startup, Enter
'   2. Crie atalho deste arquivo (.vbs) na pasta que abrir
'   3. Toda vez que o Windows iniciar, a web sobe sozinha
' ============================================================

Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Resolve diretorio do proprio .vbs (suporta atalho em qualquer lugar)
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = scriptDir & "\iniciar-web.bat"

' Run params:
'   0     = janela oculta
'   False = nao espera processo terminar (fire-and-forget)
WshShell.Run Chr(34) & batPath & Chr(34), 0, False

Set WshShell = Nothing
Set fso = Nothing
