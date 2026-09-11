# PyInstaller entry script / double-click launcher (use pythonw.exe to hide the console)
import sys
from hanwha_agent.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
