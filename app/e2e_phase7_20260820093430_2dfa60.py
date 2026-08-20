def normalize_csv(value: str) -> tuple[str, ...]:
    seen: set[str] = set()
    items: list[str] = []
    for part in value.split(","):
        item = part.strip().lower()
        if item and item not in seen:
            seen.add(item)
            items.append(item)
    return tuple(items)
