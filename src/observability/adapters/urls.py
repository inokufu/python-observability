from urllib.parse import urlsplit


def origin(url: str) -> str:
    """Keep the scheme and host of a URL: its path may hold an id or a token.

    Returns:
        The URL's origin, or the value without its query when it names no host.
    """
    parts = urlsplit(url)
    if not parts.netloc:
        return url.split(sep="?", maxsplit=1)[0]
    return f"{parts.scheme}://{parts.netloc}"
