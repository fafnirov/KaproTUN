"""KaproVPN installer — custom branded setup UI.

Ships as KaproVPN-Setup.exe alongside the portable KaproVPN.exe in each
GitHub release. The portable exe is embedded inside this installer as a
PyInstaller data file; on install it gets copied out to
%LOCALAPPDATA%\\Programs\\KaproVPN\\ and Start Menu + Desktop shortcuts
are created.
"""
