import sys
from pathlib import Path

sys.exit(0 if Path("app/result.txt").read_text(encoding="utf-8") == "valid\n" else 1)
