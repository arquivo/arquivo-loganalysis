"""Heuristic classifier: distinguishes bot/crawler User-Agents from human browsers."""
import re

_BOT_RE = re.compile(
    r'bot\b|crawler|spider|slurp|mediapartners|googlebot|bingbot|baiduspider|'
    r'yandex|duckduck|facebookexternal|ahrefsbot|semrushbot|mj12bot|petalbot|'
    r'check_http|monitoring|keepaliveclient|python-requests|python-urllib|'
    r'curl/|wget/|go-http-client|java/|headless|scrapy|httpx|apache-httpclient|'
    r'okhttp|heritrix|archive\.org',
    re.IGNORECASE,
)


def is_bot(user_agent: str) -> bool:
    """True if the User-Agent string looks like a bot / automated client."""
    if not user_agent or user_agent in ('-', ''):
        return True  # no UA → treat as bot
    return bool(_BOT_RE.search(user_agent))
