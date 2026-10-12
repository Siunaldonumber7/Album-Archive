ALBUMS_PER_PAGE = 60


def parse_page_number(value):
    try:
        return max(1, int(value))
    except Exception:
        return 1


def build_pagination_pages(current_page, total_pages, radius=2):
    total_pages = max(1, int(total_pages or 1))
    current_page = min(
        max(1, int(current_page or 1)),
        total_pages
    )

    if total_pages <= 7:
        return list(range(1, total_pages + 1))

    wanted = {1, total_pages}

    for page_number in range(
        current_page - radius,
        current_page + radius + 1
    ):
        if 1 <= page_number <= total_pages:
            wanted.add(page_number)

    ordered = sorted(wanted)
    result = []
    previous = None

    for page_number in ordered:
        if (
            previous is not None
            and page_number - previous > 1
        ):
            result.append(None)

        result.append(page_number)
        previous = page_number

    return result
