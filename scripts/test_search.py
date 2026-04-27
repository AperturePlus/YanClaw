"""Test Bing search to understand why fallback returns 0 links."""
import re
import sys
from html.parser import HTMLParser
from urllib.parse import quote

import html2text
import httpx

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

QUERY = "清华大学 师资队伍 site:tsinghua.edu.cn"
URL = f"https://www.bing.com/search?q={quote(QUERY)}&count=10"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)


def main():
    r = httpx.get(URL, headers=HEADERS, follow_redirects=True, timeout=15)
    print(f"Status: {r.status_code}")
    print(f"Content-Type: {r.headers.get('content-type', 'N/A')}")
    print(f"HTML length: {len(r.text)}")
    print()

    # Method 1: Extract from HTML text via html2text
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.body_width = 0
    text = converter.handle(r.text)
    text_urls = re.findall(r"https?://[^\s\)\]\"'>]+", text)
    tsinghua_text = [u for u in text_urls if "tsinghua.edu.cn" in u]
    print(f"Method 1 (html2text regex): {len(tsinghua_text)} tsinghua URLs")
    for u in tsinghua_text[:10]:
        print(f"  {u}")

    # Method 2: Extract from raw HTML links
    parser = LinkParser()
    parser.feed(r.text)
    tsinghua_links = [l for l in parser.links if "tsinghua.edu.cn" in l]
    print(f"\nMethod 2 (HTML <a> tags): {len(tsinghua_links)} tsinghua links")
    for l in tsinghua_links[:10]:
        print(f"  {l}")

    # Method 3: Regex on raw HTML
    raw_urls = re.findall(r'https?://[^"\'<>\s]+tsinghua\.edu\.cn[^"\'<>\s]*', r.text)
    print(f"\nMethod 3 (raw HTML regex): {len(raw_urls)} tsinghua URLs")
    for u in list(dict.fromkeys(raw_urls))[:10]:
        print(f"  {u}")

    # Show a snippet of the text to understand structure
    print(f"\n--- Text snippet (first 2000 chars) ---")
    print(text[:2000])


if __name__ == "__main__":
    main()
