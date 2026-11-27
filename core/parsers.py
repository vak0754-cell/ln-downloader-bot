from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, unquote
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Tag

DIRECT_EXTS = {".epub", ".pdf", ".txt", ".fb2", ".zip"}


@dataclass
class ChapterRef:
    index: int
    title: str
    url: str


@dataclass
class SourcePlan:
    mode: str  # "direct_file" | "directory" | "web_book"
    source_url: str
    title: str
    author: str = "Unknown"
    cover_url: str = ""
    chapters: list[ChapterRef] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]+', "", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:100] or "book"


def parse_range_spec(spec: str, total: int) -> list[int]:
    """Parse range strings like '1-50', '10,12,15-20', or 'all' into 1-based sorted indices."""
    spec = spec.strip().lower()
    if spec in {"all", "всі", "усі", "*"}:
        return list(range(1, total + 1))
    picked: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.strip().isdigit() and b.strip().isdigit():
                start, end = int(a.strip()), int(b.strip())
                for i in range(max(1, start), min(total, end) + 1):
                    picked.add(i)
        elif part.isdigit():
            idx = int(part)
            if 1 <= idx <= total:
                picked.add(idx)
    return sorted(picked)


class PoliteHttpClient:
    """Standard async HTTP client with retries, rate limiting, and robots.txt awareness."""

    def __init__(self, user_agent: str, timeout: int = 25, rate_delay: float = 0.5) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.rate_delay = rate_delay
        self._robots_cache: dict[str, RobotFileParser | None] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "uk,en-US;q=0.9,en;q=0.8",
        }

    async def check_robots(self, client: httpx.AsyncClient, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            rp = RobotFileParser()
            try:
                resp = await client.get(f"{origin}/robots.txt", timeout=8)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                    self._robots_cache[origin] = rp
                else:
                    self._robots_cache[origin] = None
            except Exception:
                self._robots_cache[origin] = None
        rp = self._robots_cache[origin]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    async def get_text(self, client: httpx.AsyncClient, url: str) -> tuple[str, str]:
        if not await self.check_robots(client, url):
            raise RuntimeError("Сайт забороняє автоматичне сканування цієї сторінки через robots.txt.")
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    await asyncio.sleep(1.5 * attempt)
                resp = await client.get(url, headers=self._headers(), follow_redirects=True, timeout=self.timeout)
                if resp.status_code in {403, 503} and ("cloudflare" in resp.headers.get("server", "").lower() or "just a moment" in resp.text.lower()):
                    raise RuntimeError(
                        "Сайт захищено Cloudflare / антибот-перевіркою. "
                        "Бот працює лише зі стандартними відкритими сайтами без обходу захисту."
                    )
                resp.raise_for_status()
                return resp.text, str(resp.url)
            except RuntimeError:
                raise
            except Exception as exc:
                last_err = exc
        raise RuntimeError(f"Не вдалося відкрити {url}: {last_err}")

    async def download_binary(self, client: httpx.AsyncClient, url: str, dest_dir: Path, max_mb: float = 48.0) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        max_bytes = int(max_mb * 1024 * 1024)
        async with client.stream("GET", url, headers=self._headers(), follow_redirects=True, timeout=60) as resp:
            if resp.status_code == 403:
                raise RuntimeError("HTTP 403: сервер заблокував пряме завантаження.")
            resp.raise_for_status()
            cd = resp.headers.get("content-disposition", "")
            m = re.search(r'filename="?([^";]+)"?', cd)
            fname = m.group(1) if m else Path(unquote(urlparse(str(resp.url)).path)).name
            fname = sanitize_filename(fname or "downloaded_file.epub")
            dest = dest_dir / fname
            written = 0
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    written += len(chunk)
                    if written > max_bytes:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        raise RuntimeError(f"Файл перевищує ліміт {max_mb} МБ.")
                    fh.write(chunk)
            return dest


def _extract_meta(soup: BeautifulSoup, url: str) -> tuple[str, str, str]:
    title = ""
    author = "Unknown"
    cover = ""

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    if not title and soup.find("h1"):
        title = soup.find("h1").get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)

    meta_author = soup.find("meta", attrs={"name": "author"})
    if meta_author and meta_author.get("content"):
        author = meta_author["content"].strip()
    else:
        a_rel = soup.find("a", attrs={"rel": "author"})
        if a_rel:
            author = a_rel.get_text(" ", strip=True)

    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        cover = urljoin(url, og_img["content"].strip())

    return title or Path(urlparse(url).path).name or "Untitled", author, cover


def _parse_royalroad(soup: BeautifulSoup, base_url: str) -> list[ChapterRef]:
    chapters: list[ChapterRef] = []
    for tr in soup.select("table#chapters tbody tr"):
        a = tr.find("a", href=True)
        if a:
            chapters.append(ChapterRef(len(chapters) + 1, a.get_text(" ", strip=True), urljoin(base_url, a["href"])))
    return chapters


def _parse_generic_chapters(soup: BeautifulSoup, base_url: str) -> list[ChapterRef]:
    """Generic chapter list extractor for open web-fiction pages."""
    parsed_base = urlparse(base_url)
    seen: set[str] = set()
    chapters: list[ChapterRef] = []

    # Priority containers
    containers = soup.select(
        ".chapter-list, #chapters, .chapters, .toc, .index, [class*='chapter-list'], [id*='chapter']"
    ) or [soup]

    chapter_hint_re = re.compile(r"(chapter|ch[-_/]|episode|ep[-_/]|vol[-_/]|part[-_/]|розд|глава|том)", re.IGNORECASE)

    for container in containers:
        for a in container.find_all("a", href=True):
            href = urljoin(base_url, a["href"].split("#", 1)[0])
            if href in seen or href == base_url:
                continue
            p = urlparse(href)
            if p.netloc != parsed_base.netloc:
                continue
            text = a.get_text(" ", strip=True)
            if not text or len(text) > 160:
                continue
            if containers != [soup] or chapter_hint_re.search(href) or chapter_hint_re.search(text):
                seen.add(href)
                chapters.append(ChapterRef(len(chapters) + 1, text, href))
        if chapters:
            break

    return chapters


async def inspect_url(http: PoliteHttpClient, url: str) -> SourcePlan:
    """Analyze any URL and classify it as direct_file, directory listing, or web_book."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Посилання має починатися з http:// або https://")

    # 1. Direct file by extension
    ext = Path(unquote(parsed.path)).suffix.lower()
    if ext in DIRECT_EXTS:
        fname = unquote(Path(parsed.path).name)
        return SourcePlan(mode="direct_file", source_url=url, title=Path(fname).stem)

    async with httpx.AsyncClient() as client:
        raw_html, final_url = await http.get_text(client, url)

    soup = BeautifulSoup(raw_html, "lxml")

    # 2. Check for open directory listing of ebook files (.epub/.pdf/.txt/.fb2/.zip)
    dir_files: list[dict[str, str]] = []
    seen_f: set[str] = set()
    for a in soup.find_all("a", href=True):
        full = urljoin(final_url, a["href"])
        fext = Path(unquote(urlparse(full).path)).suffix.lower()
        if fext in DIRECT_EXTS and full not in seen_f:
            seen_f.add(full)
            name = unquote(Path(urlparse(full).path).name) or a.get_text(" ", strip=True)
            dir_files.append({"name": name, "url": full, "ext": fext})
    if len(dir_files) >= 1:
        title = unquote(Path(urlparse(final_url).path.rstrip("/")).name) or urlparse(final_url).netloc
        return SourcePlan(mode="directory", source_url=final_url, title=title, files=dir_files)

    # 3. Web book / chaptered page
    title, author, cover = _extract_meta(soup, final_url)
    host = urlparse(final_url).netloc.lower()
    if "royalroad.com" in host:
        chapters = _parse_royalroad(soup, final_url)
    else:
        chapters = _parse_generic_chapters(soup, final_url)

    # Fallback: treat the page itself as a single chapter/story
    if not chapters:
        chapters = [ChapterRef(1, title, final_url)]

    return SourcePlan(
        mode="web_book",
        source_url=final_url,
        title=title,
        author=author,
        cover_url=cover,
        chapters=chapters,
    )


def extract_clean_chapter_html(raw_html: str) -> tuple[str, str]:
    """Extract clean title and main narrative <p> blocks from a chapter HTML page."""
    soup = BeautifulSoup(raw_html, "lxml")
    for junk in soup.select("script, style, nav, header, footer, aside, form, iframe, .ads, .advertisement, .comments"):
        junk.decompose()

    h = soup.find(["h1", "h2"])
    ch_title = h.get_text(" ", strip=True) if h else ""

    # Known content selectors first
    known = soup.select_one(
        ".chapter-inner, .chapter-content, #chapter-content, .entry-content, article .content, article, main"
    )
    candidate: Tag | None = known

    # Heuristic fallback: pick container with the largest total paragraph text
    if candidate is None or len(candidate.find_all("p")) < 3:
        best_score = 0
        for tag in soup.find_all(["div", "article", "section", "main"]):
            ps = tag.find_all("p", recursive=False)
            score = sum(len(p.get_text(" ", strip=True)) for p in ps)
            if score > best_score:
                best_score = score
                candidate = tag

    paragraphs: list[str] = []
    if candidate:
        for p in candidate.find_all(["p", "blockquote"]):
            txt = p.get_text(" ", strip=True)
            if txt and len(txt) > 1:
                paragraphs.append(f"<p>{html.escape(txt)}</p>")

    if not paragraphs:
        body = soup.body or soup
        for line in body.get_text("\n").splitlines():
            line = line.strip()
            if len(line) > 20:
                paragraphs.append(f"<p>{html.escape(line)}</p>")

    return ch_title, "\n".join(paragraphs)
