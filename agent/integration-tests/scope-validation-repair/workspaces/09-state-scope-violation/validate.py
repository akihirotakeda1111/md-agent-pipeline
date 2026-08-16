from pathlib import Path

Path("VALIDATION_MUST_NOT_RUN").write_text("ran", encoding="utf-8")
