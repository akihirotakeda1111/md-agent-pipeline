def normalize_csv(value: str) -> tuple[str, ...]:
    seen: set[str] = set()
    items: list[str] = []

    for raw_item in value.split(","):
        item = raw_item.strip().lower()
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)

    return tuple(items)
