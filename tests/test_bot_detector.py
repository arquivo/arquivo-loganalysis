import pytest
from app.bot_detector import is_bot


@pytest.mark.parametrize('ua', [
    'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
    'Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)',
    'Mozilla/5.0 (compatible; YandexBot/3.0)',
    'Mozilla/5.0 (compatible; AhrefsBot/7.0)',
    'Mozilla/5.0 (compatible; SemrushBot/7~bl)',
    'facebookexternalhit/1.1',
    'curl/7.88',
    'Wget/1.21',
    'python-requests/2.31',
    'Go-http-client/1.1',
    'Java/17',
    'KeepAliveClient',
    'check_http/v2.2',
    'archive.org_bot',
    'Scrapy/2.11',
    'HeadlessChrome/120',
])
def test_detects_bots(ua):
    assert is_bot(ua)


@pytest.mark.parametrize('ua', [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/121.0',
])
def test_detects_humans(ua):
    assert not is_bot(ua)


def test_empty_ua_is_bot():
    assert is_bot('')
    assert is_bot('-')
    assert is_bot(None)
