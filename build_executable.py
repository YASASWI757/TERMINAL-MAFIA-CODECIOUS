"""
Builds the single-file Terminal Mafia executable for the "Executable
File" deliverable.

PyInstaller does NOT cross-compile: run this ON the platform you want
the executable for (Linux, Mac, or Windows each need their own run).
Whichever machine you run this on produces the executable for that
machine's platform.

Usage:
    pip install pyinstaller
    python build_executable.py

Output:
    dist/terminal-mafia        (Linux / Mac)
    dist/terminal-mafia.exe    (Windows)

That one file is both the server and the client -- see launcher.py's
docstring, or just run it with no arguments for an interactive menu:
    ./dist/terminal-mafia                 # menu-driven
    ./dist/terminal-mafia server --bots 3 # host a game directly
    ./dist/terminal-mafia client --name Alice --host <ip>
"""

import subprocess
import sys

def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller isn't installed. Run: pip install pyinstaller")
        sys.exit(1)

    subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "terminal-mafia",
        "--clean",
        "launcher.py",
    ], check=True)

    print("\nBuild complete. The executable is in dist/")
    print("Run it directly -- no Python installation needed on the machine you copy it to.")


if __name__ == "__main__":
    main()
