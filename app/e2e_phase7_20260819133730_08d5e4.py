def normalize_csv(value: str) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []

    for item in value.split(","):
        item = item.strip().lower()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)

    return tuple(normalized)
