Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Bionic Computer\OneDrive\Desktop\gis_tile_cutter"
WshShell.Run "python main.py", 1, False
