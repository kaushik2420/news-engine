"""RSS builder shared by the offline fixtures."""
from xml.sax.saxutils import escape


def rss(title: str, items: list[dict]) -> str:
    entries = "".join(f"""<item><title>{escape(i['title'])}</title><link>{escape(i['link'])}</link>
<pubDate>{i['date']}</pubDate><description>{escape(i['summary'])}</description></item>""" for i in items)
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{title}</title>{entries}</channel></rss>'
