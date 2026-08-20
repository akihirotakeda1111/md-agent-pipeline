def normalize_csv(value: str) -> tuple[str, ...]:
    seen: set[str] = set()
    items: list[str] = []

    for part in value.split(","):
        item = part.strip().lower()
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)

    return tuple(items)
