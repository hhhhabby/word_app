import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


QUIJI_LOOKUP_URL = "https://qujidanci.xieyonglin.com/api/word/lookup.php"


def _session_with_retry():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_quji_word_data(word):
    """Fetch full word metadata from 趣记单词."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    response = _session_with_retry().get(
        QUIJI_LOOKUP_URL,
        params={"word": word},
        headers=headers,
        timeout=15,
        verify=False,
    )
    response.raise_for_status()
    return response.json()


def get_quji_help(word):
    """Return pinyin/homophone memory tips for one English word."""
    try:
        json_data = fetch_quji_word_data(word)
        memory_tips = json_data.get("data", {}).get("memory_tips", [])
        results = []
        for tip in memory_tips:
            method = tip.get("method", "")
            details = tip.get("details", "")
            if "谐音" in method and details:
                results.append(details)

        if results:
            return "；".join(results)
        return "未找到谐音助记"
    except Exception as exc:
        return f"查询失败：{str(exc)[:80]}"
