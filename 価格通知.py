import os
import re
import html
import asyncio
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, Response, PlainTextResponse
from pydantic import BaseModel

from sqlalchemy import create_engine, String, Integer, Text, DateTime, UniqueConstraint, select, desc, func
from sqlalchemy.orm import declarative_base, Mapped, mapped_column, sessionmaker

import feedparser
import httpx
from bs4 import BeautifulSoup  # 予備
from selectolax.parser import HTMLParser
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =========================
# 環境変数
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TARGET_LANG = os.getenv("TARGET_LANG", "ja")
SUMMARY_TONE = os.getenv("SUMMARY_TONE", "投資家向けに、価格影響と重要ファクトを3〜5行で要約")
SITE_TITLE = os.getenv("SITE_TITLE", "Crypto News Agent")
if not OPENAI_API_KEY:
    print("⚠️ OPENAI_API_KEY が未設定です。要約はフォールバックになります。")

# =========================
# OpenAI クライアント
# =========================
try:
    from openai import OpenAI
    oai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    oai_client = None

SYSTEM_PROMPT = f"""あなたは暗号資産ニュースの専門アナリストです。
出力は{TARGET_LANG}。煽りは禁止。数値・日付・固有名詞は正確に。
企業・トークン・規制・相場影響（強弱/短中長期）を簡潔に整理してください。"""

def summarize(title: str, url: str, raw_snippet: str) -> str:
    """OpenAIで日本語要約（失敗時は簡易サマリにフォールバック）"""
    prompt = f"""次の記事を{TARGET_LANG}で{SUMMARY_TONE}。
- タイトル: {title}
- URL: {url}
- 抜粋/説明: {raw_snippet or '（抜粋なし）'}
- 形式: 箇条書き3-5点 + 最後に「一言見立て：...」
"""
    try:
        if not oai_client:
            raise RuntimeError("OpenAI client not ready")
        resp = oai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        # フォールバック（最低限の要約）
        return f"- {title}\n- {url}\n一言見立て：詳細不明。リンク先を参照。"

# =========================
# DB（SQLite / SQLAlchemy 2.x）
# =========================
Base = declarative_base()
engine = create_engine("sqlite:///news.db", future=True, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

class Article(Base):
    __tablename__ = "articles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50), index=True)       # "coindesk" / "coinpost"
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(String(1024), unique=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    summary_ja: Mapped[str] = mapped_column(Text)
    categories: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint('url', name='uq_article_url'),)

def init_db():
    Base.metadata.create_all(bind=engine)

# =========================
# ユーティリティ
# =========================
def parse_pubdate(dt) -> datetime:
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(str(dt))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CryptoNewsAgent/1.0)"}

# =========================
# フィード取得（RSS優先、CoinPostはフォールバック）
# =========================
COINDESK_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"
COINPOST_RSS_CANDIDATE = "https://coinpost.jp/?feed=rss2"
COINPOST_HOME = "https://coinpost.jp/"

async def fetch_rss_entries(url: str):
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        r = await client.get(url)
        r.raise_for_status()
    d = feedparser.parse(r.text)
    for e in d.entries:
        yield {
            "title": (e.get("title") or "").strip(),
            "link": (e.get("link") or "").strip(),
            "published": parse_pubdate(e.get("published") or e.get("updated") or datetime.now(timezone.utc)),
            "summary": (e.get("summary") or "").strip(),
            "tags": ",".join(t["term"] for t in e.get("tags", []) if t.get("term"))
        }

async def fetch_coindesk():
    async for item in fetch_rss_entries(COINDESK_RSS):
        item["source"] = "coindesk"
        yield item

async def fetch_coinpost():
    # 1) RSSに挑戦
    try:
        async for item in fetch_rss_entries(COINPOST_RSS_CANDIDATE):
            item["source"] = "coinpost"
            yield item
        return
    except Exception:
        pass
    # 2) HTMLフォールバック（新着一覧）
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        r = await client.get(COINPOST_HOME)
        r.raise_for_status()
    htmlp = HTMLParser(r.text)
    seen = set()
    for a in htmlp.css("a"):
        href = a.attributes.get("href", "")
        title = a.text(strip=True)
        if not href or not title:
            continue
        if not href.startswith("https://coinpost.jp/"):
            continue
        # 記事URLらしいパターン（例：/p/123456/ or /2025/10/18/xxxxx/）
        if not re.search(r"/\d+/$|/p/\d+", href):
            continue
        if href in seen:
            continue
        seen.add(href)
        yield {
            "source": "coinpost",
            "title": title[:200],
            "link": href,
            "published": datetime.now(timezone.utc),
            "summary": "",
            "tags": ""
        }

# =========================
# ハーベスト & 要約（保存）
# =========================
async def harvest_and_summarize():
    sources = [fetch_coindesk(), fetch_coinpost()]
    for agen in sources:
        async for item in agen:
            url = item["link"]
            if not url:
                continue
            with SessionLocal() as s:
                exists = s.scalar(select(Article).where(Article.url == url).limit(1))
                if exists:
                    continue
                summary = summarize(item["title"], url, item.get("summary", ""))
                a = Article(
                    source=item["source"],
                    title=item["title"],
                    url=url,
                    published_at=item["published"],
                    summary_ja=summary,
                    categories=item.get("tags", "")
                )
                s.add(a)
                try:
                    s.commit()
                except Exception:
                    s.rollback()

# =========================
# FastAPI（API + HTML + RSS）
# =========================
class ArticleOut(BaseModel):
    id: int
    source: str
    title: str
    url: str
    published_at: datetime
    summary_ja: str
    categories: str
    class Config:
        from_attributes = True

app = FastAPI(title=SITE_TITLE)

@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(harvest_and_summarize())
    scheduler = AsyncIOScheduler()
    scheduler.add_job(harvest_and_summarize, "interval", minutes=5, next_run_time=None)
    scheduler.start()
    app.state.scheduler = scheduler

@app.get("/health")
async def health():
    return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}

@app.get("/news", response_model=List[ArticleOut])
def list_news(source: Optional[str] = Query(None), limit: int = Query(30, ge=1, le=200), page: int = Query(1, ge=1)):
    offset = (page - 1) * limit
    with SessionLocal() as s:
        base = select(Article)
        if source:
            base = base.where(Article.source == source)
        total = s.scalar(select(func.count()).select_from(base.subquery()))
        stmt = base.order_by(desc(Article.published_at)).offset(offset).limit(limit)
        items = s.scalars(stmt).all()
        return items

@app.get("/", response_class=HTMLResponse)
def homepage(source: Optional[str] = Query(None), page: int = Query(1, ge=1), limit: int = Query(20, ge=5, le=100), auto: int = Query(60, ge=0, le=600)):
    """シンプルなHTMLのトップページ（Tailwind CDNなし、純CSS）。auto: N秒ごとに自動リロード（0で無効）。"""
    offset = (page - 1) * limit
    with SessionLocal() as s:
        base = select(Article)
        if source:
            base = base.where(Article.source == source)
        total = s.scalar(select(func.count()).select_from(base.subquery()))
        stmt = base.order_by(desc(Article.published_at)).offset(offset).limit(limit)
        rows = s.scalars(stmt).all()

    total_pages = max((total + limit - 1) // limit, 1)
    def esc(x): return html.escape(str(x)) if x is not None else ""
    # シンプルCSS + オートリロード
    meta_refresh = f'<meta http-equiv="refresh" content="{auto}">' if auto and auto > 0 else ""
    html_page = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{meta_refresh}
<title>{esc(SITE_TITLE)}</title>
<style>
  :root {{ --bg:#0f1220; --card:#181c2f; --text:#e9ecff; --muted:#a2a8c7; --acc:#59e0b7; }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif; background:linear-gradient(180deg,#0b0e18, #0f1220); color:var(--text) }}
  header {{ padding:16px 20px; border-bottom:1px solid #22263b; display:flex; gap:12px; align-items:center; flex-wrap:wrap }}
  header h1 {{ margin:0; font-size:20px }}
  header a {{ color:var(--acc); text-decoration:none }}
  .wrap {{ max-width:980px; margin:20px auto; padding:0 16px }}
  .toolbar {{ display:flex; gap:8px; align-items:center; color:var(--muted); margin-bottom:12px }}
  .toolbar a.btn {{ padding:6px 10px; border:1px solid #2a3150; border-radius:8px; color:var(--text); text-decoration:none }}
  .grid {{ display:grid; grid-template-columns:1fr; gap:12px }}
  .card {{ background:var(--card); border:1px solid #242a45; border-radius:14px; padding:16px; box-shadow:0 4px 12px rgba(0,0,0,.25) }}
  .card h3 {{ margin:0 0 8px; font-size:18px }}
  .meta {{ color:var(--muted); font-size:12px; margin-bottom:8px }}
  .summary {{ white-space:pre-wrap; line-height:1.5 }}
  .pager {{ display:flex; gap:8px; justify-content:center; margin:18px 0 }}
  .pager a {{ color:var(--text); text-decoration:none; border:1px solid #2a3150; padding:6px 10px; border-radius:8px }}
  .filter a.active {{ background:#243056; border-color:#32406f }}
  footer {{ color:var(--muted); font-size:12px; text-align:center; padding:24px 0 32px }}
  @media (min-width: 800px) {{
    .grid {{ grid-template-columns:1fr 1fr }}
  }}
</style>
</head>
<body>
<header>
  <h1>📰 {esc(SITE_TITLE)}</h1>
  <div class="filter">
    <a class="btn {'active' if not source else ''}" href="/?page=1&limit={limit}&auto={auto}">All</a>
    <a class="btn {'active' if source=='coindesk' else ''}" href="/?source=coindesk&page=1&limit={limit}&auto={auto}">CoinDesk</a>
    <a class="btn {'active' if source=='coinpost' else ''}" href="/?source=coinpost&page=1&limit={limit}&auto={auto}">CoinPost</a>
  </div>
  <div class="toolbar" style="margin-left:auto;">
    <a class="btn" href="/rss.xml">RSS</a>
    <a class="btn" href="/news?limit={limit}{'&source='+source if source else ''}">JSON</a>
    <span>Auto-refresh: {auto}s</span>
  </div>
</header>

<div class="wrap">
  <div class="grid">
    {"".join([
      f'''
      <article class="card">
        <h3><a href="{esc(r.url)}" target="_blank" rel="noopener">{esc(r.title)}</a></h3>
        <div class="meta">{esc(r.source)} ｜ {r.published_at.strftime('%Y-%m-%d %H:%M')} UTC</div>
        <div class="summary">{esc(r.summary_ja)}</div>
      </article>
      ''' for r in rows
    ]) or '<p>まだ記事がありません。数分後に自動更新されます。</p>'}
  </div>

  <nav class="pager">
    {"<a href='/?{q}'>« Prev</a>".format(q=f"source={source}&page={page-1}&limit={limit}&auto={auto}" if source else f"page={page-1}&limit={limit}&auto={auto}") if page>1 else ""}
    <span style="padding:6px 10px;">{page} / {total_pages}</span>
    {"<a href='/?{q}'>Next »</a>".format(q=f"source={source}&page={page+1}&limit={limit}&auto={auto}" if page<total_pages else f"page={total_pages}&limit={limit}&auto={auto}") if page<total_pages else ""}
  </nav>
</div>

<footer>
  <div>データ元: CoinDesk / CoinPost（要約は自動生成・投資判断は自己責任）</div>
</footer>
</body>
</html>
"""
    return HTMLResponse(html_page)

@app.get("/rss.xml")
def rss(feed_title: str = Query(default=SITE_TITLE), feed_link: str = Query(default=""), feed_desc: str = Query(default="最新の要約済みクリプトニュース")):
    """自サイト用RSSフィード（最新50件）"""
    with SessionLocal() as s:
        stmt = select(Article).order_by(desc(Article.published_at)).limit(50)
        items = s.scalars(stmt).all()

    now = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
    link = feed_link or "http://localhost:8000/"
    def esc(x): return html.escape(str(x)) if x is not None else ""

    items_xml = []
    for a in items:
        pub = a.published_at.strftime("%a, %d %b %Y %H:%M:%S GMT")
        items_xml.append(f"""
    <item>
      <title>{esc(a.title)}</title>
      <link>{esc(a.url)}</link>
      <guid isPermaLink="false">{esc(a.url)}</guid>
      <pubDate>{pub}</pubDate>
      <description>{esc(a.summary_ja)}</description>
      <category>{esc(a.source)}</category>
    </item>""")

    rss_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
 <channel>
  <title>{esc(feed_title)}</title>
  <link>{esc(link)}</link>
  <description>{esc(feed_desc)}</description>
  <language>ja</language>
  <lastBuildDate>{now}</lastBuildDate>
  {''.join(items_xml)}
 </channel>
</rss>"""
    return Response(content=rss_xml, media_type="application/rss+xml")

# ローカル起動用
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
