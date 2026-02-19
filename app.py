# -*- coding: utf-8 -*-
"""
DocCrawler Pro（诊断增强版）
- 解决“看不出失败原因”的问题：把入口页状态码/robots拦截/sitemap/mkdocs索引等关键诊断写入 SSE 日志
- UI 增加：
  1) 正文 CSS 选择器（可选）
  2) 是否尊重 robots.txt（默认开，可关闭）
  3) 是否限制到起始路径前缀（默认开，可关闭）
- MkDocs 站点：优先尝试 /search/search_index.json 抽取全站 location
"""

from __future__ import annotations

import os
import re
import time
import json
import uuid
import gzip
import logging
import threading
import random
from dataclasses import dataclass, replace
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Tuple, List, Dict, Set
from urllib.parse import (
    urljoin, urlparse, urldefrag, urlunparse, parse_qsl, urlencode
)
from urllib.robotparser import RobotFileParser
from queue import Queue, Empty

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from flask import (
    Flask, request, Response, send_file, send_from_directory,
    after_this_request, stream_with_context, jsonify
)

# -----------------------------
# 配置
# -----------------------------
@dataclass
class CrawlerConfig:
    TEMP_DIR: str = "temp_docs"
    MAX_WORKERS: int = 6

    MAX_SCAN_PAGES: int = 800
    MAX_DEPTH: int = 5
    MAX_PAGES_TO_FETCH: int = 1200

    REQUEST_TIMEOUT: int = 15
    RETRY_TIMES: int = 3
    RETRY_SLEEP_BASE: float = 1.0

    RESTRICT_TO_START_PATH_PREFIX: bool = True
    RESPECT_ROBOTS: bool = True

    PER_HOST_DELAY: float = 0.2

    MIN_MD_LEN: int = 80
    STRIP_MARKDOWN_LINKS: bool = True
    DROP_IMAGES: bool = True
    INCLUDE_SOURCE_LINE: bool = False

    MAX_HTML_BYTES: int = 2_500_000
    ALLOW_CONTENT_TYPE: tuple = ("text/html", "application/xhtml+xml")

    SKIP_EXTS: tuple = (
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
        ".css", ".js", ".map",
        ".pdf", ".zip", ".rar", ".7z", ".tar",
        ".mp4", ".mp3", ".avi", ".mov", ".mkv",
        ".woff", ".woff2", ".ttf", ".eot",
    )

    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36 DocCrawlerPro/3.1"
    )

    FILE_TTL_SECONDS: int = 1800
    MAX_SELECTOR_LEN: int = 200


CFG = CrawlerConfig()
os.makedirs(CFG.TEMP_DIR, exist_ok=True)
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("DocCrawlerPro")


# -----------------------------
# Task 日志（关键：把失败原因落地）
# -----------------------------
class TaskLogger:
    def __init__(self, log_path: str, max_lines: int = 2000):
        self.log_path = log_path
        self.buf = deque(maxlen=max_lines)
        self.lock = threading.Lock()
        # 先清空
        try:
            with open(self.log_path, "w", encoding="utf-8") as f:
                f.write("")
        except Exception:
            pass

    def write(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self.lock:
            self.buf.append(line)
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def tail(self, n: int = 200) -> List[str]:
        with self.lock:
            return list(self.buf)[-n:]


# -----------------------------
# Markdown 清洗
# -----------------------------
_MD_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_REF_LINK_RE = re.compile(r"\[([^\]]+)\]\[([^\]]*)\]")
_MD_REF_DEF_RE = re.compile(r"^\s*\[[^\]]+\]:\s+\S+.*$", re.MULTILINE)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")

def strip_markdown_links(markdown_text: str) -> str:
    if not markdown_text:
        return markdown_text
    markdown_text = _MD_REF_DEF_RE.sub("", markdown_text)
    markdown_text = _MD_INLINE_LINK_RE.sub(r"\1", markdown_text)
    markdown_text = _MD_REF_LINK_RE.sub(r"\1", markdown_text)
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text).strip()
    return markdown_text

def cleanup_markdown(markdown_text: str, cfg: CrawlerConfig) -> str:
    if not markdown_text:
        return ""
    if cfg.DROP_IMAGES:
        markdown_text = _MD_IMAGE_RE.sub("", markdown_text)
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text).strip()
    if cfg.STRIP_MARKDOWN_LINKS:
        markdown_text = strip_markdown_links(markdown_text)
    return markdown_text


# -----------------------------
# HTML 工具
# -----------------------------
def make_soup(html: str, kind: str = "html") -> BeautifulSoup:
    if kind == "xml":
        for parser in ("xml", "lxml-xml", "lxml"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return BeautifulSoup(html, "html.parser")

    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")

def remove_noise_nodes_global(soup: BeautifulSoup):
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "canvas"]):
        tag.decompose()

    selectors = [
        "nav", "footer", "aside",
        ".sidebar", ".side-bar", ".sphinxsidebar", ".wy-nav-side",
        ".toc", "#toc", ".table-of-contents", "#table-of-contents",
        ".breadcrumbs", ".breadcrumb", ".pagination",
        ".headerlink", ".site-header", ".site-footer",
        ".search", "#search", ".searchbox", "#searchbox",
        ".navbar", ".topbar", ".bottom-nav",
        ".md-nav", ".md-sidebar", ".md-header", ".md-footer", ".md-search",
        ".rst-footer-buttons",
    ]
    for sel in selectors:
        for node in soup.select(sel):
            node.decompose()

def remove_noise_nodes_local(container: BeautifulSoup):
    for tag in container(["script", "style", "noscript", "iframe", "svg", "canvas"]):
        tag.decompose()

    selectors = [
        "nav", "footer", "aside",
        ".toc", "#toc", ".table-of-contents", "#table-of-contents",
        ".breadcrumbs", ".breadcrumb", ".pagination",
        ".search", "#search", ".searchbox", "#searchbox",
        ".rst-footer-buttons",
        ".headerlink",
    ]
    for sel in selectors:
        for node in container.select(sel):
            node.decompose()

def pick_main_content(soup: BeautifulSoup, override_selector: Optional[str] = None) -> Optional[BeautifulSoup]:
    if override_selector:
        try:
            node = soup.select_one(override_selector)
            if node and node.get_text(strip=True):
                return node
        except Exception:
            pass

    candidates = [
        "article.md-content__inner",
        ".md-content__inner",
        "article",
        "main",
        ".markdown-body",
        ".document",
        ".content",
        "#content",
        ".bd-content",
        "div[role='main']",
        ".prose",
        "body",
    ]
    for sel in candidates:
        try:
            node = soup.select_one(sel)
        except Exception:
            node = None
        if node and node.get_text(strip=True):
            return node
    return None

def absolutize_links_in_content(content: BeautifulSoup, base_url: str):
    for tag in content.find_all("a", href=True):
        href = tag.get("href")
        if href and not href.startswith(("http://", "https://", "mailto:", "javascript:", "#", "data:")):
            tag["href"] = urljoin(base_url, href)
    for tag in content.find_all("img", src=True):
        src = tag.get("src")
        if src and not src.startswith(("http://", "https://", "data:")):
            tag["src"] = urljoin(base_url, src)


# -----------------------------
# URL 工具（关键：目录 URL 统一尾斜杠）
# -----------------------------
_TRACKING_QS_PREFIX = ("utm_",)
_TRACKING_QS_KEYS = ("spm", "gclid", "fbclid")

def ensure_dir_url(url: str) -> str:
    p = urlparse(url)
    path = p.path or "/"
    seg = path.rsplit("/", 1)[-1]
    if seg and "." not in seg and not path.endswith("/"):
        path = path + "/"
    if not path:
        path = "/"
    return urlunparse((p.scheme, p.netloc, path, p.params, p.query, ""))

def normalize_url(url: str) -> str:
    if not url:
        return ""
    url, _ = urldefrag(url)
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return ""

    path = p.path or "/"
    seg = path.rsplit("/", 1)[-1]
    if seg and "." not in seg and not path.endswith("/"):
        path = path + "/"
    if not path:
        path = "/"

    qs = parse_qsl(p.query, keep_blank_values=True)
    cleaned = []
    for k, v in qs:
        lk = (k or "").lower()
        if lk.startswith(_TRACKING_QS_PREFIX) or lk in _TRACKING_QS_KEYS:
            continue
        cleaned.append((k, v))
    new_query = urlencode(cleaned, doseq=True)

    netloc = (p.netloc or "").lower()
    return urlunparse((p.scheme.lower(), netloc, path, p.params, new_query, ""))


# -----------------------------
# 核心爬虫
# -----------------------------
class WebCrawler:
    def __init__(self, start_url: str, task_id: str, progress_queue: Queue, cfg: CrawlerConfig,
                 task_logger: TaskLogger,
                 content_selector: Optional[str] = None):
        self.cfg = cfg
        self.task_id = task_id
        self.progress_queue = progress_queue
        self.tlog = task_logger

        self.start_url_raw = start_url.strip()
        self.start_url = ensure_dir_url(self.start_url_raw)
        self.content_selector = (content_selector or "").strip() or None

        parsed = urlparse(self.start_url)
        self.base_domain = parsed.netloc
        self.base_scheme = parsed.scheme
        self.base_url_full = f"{self.base_scheme}://{self.base_domain}"

        self.start_path_prefix = parsed.path or "/"
        if not self.start_path_prefix.endswith("/"):
            self.start_path_prefix = self.start_path_prefix.rsplit("/", 1)[0] + "/"
        if self.start_path_prefix == "//":
            self.start_path_prefix = "/"
        self.start_path_prefix_noslash = self.start_path_prefix.rstrip("/") or "/"

        self.output_filename = f"官方文档_{task_id}.md"
        self.output_path = os.path.join(self.cfg.TEMP_DIR, self.output_filename)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.cfg.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Connection": "keep-alive",
        })

        self.visited_lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.visited_normalized: Set[str] = set()

        self.rp: Optional[RobotFileParser] = None

        self._rate_lock = threading.Lock()
        self._last_req_ts = 0.0

        with open(self.output_path, "w", encoding="utf-8") as f:
            f.write("# 项目文档全集\n\n")
            f.write(f"> 起始地址: {self.start_url}\n")
            f.write(f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"> 路径前缀限制: {self.cfg.RESTRICT_TO_START_PATH_PREFIX} ({self.start_path_prefix})\n")
            f.write(f"> 尊重 robots.txt: {self.cfg.RESPECT_ROBOTS}\n")
            if self.content_selector:
                f.write(f"> 正文选择器: {self.content_selector}\n")
            f.write("\n")

    def log_progress(self, percent: int, message: str, status: str = "processing"):
        percent = max(0, min(100, int(percent)))
        self.tlog.write(f"{percent:>3}% [{status}] {message}")
        self.progress_queue.put({"percent": percent, "message": message, "status": status})

    def _polite_delay(self):
        if self.cfg.PER_HOST_DELAY <= 0:
            return
        with self._rate_lock:
            now = time.time()
            gap = now - self._last_req_ts
            if gap < self.cfg.PER_HOST_DELAY:
                time.sleep(self.cfg.PER_HOST_DELAY - gap)
            self._last_req_ts = time.time()

    def is_same_domain(self, url: str) -> bool:
        try:
            return urlparse(url).netloc.lower() == self.base_domain.lower()
        except Exception:
            return False

    def is_under_prefix(self, url: str) -> bool:
        if not self.cfg.RESTRICT_TO_START_PATH_PREFIX:
            return True
        try:
            path = urlparse(url).path or "/"
            return path.startswith(self.start_path_prefix) or (path.rstrip("/") == self.start_path_prefix_noslash)
        except Exception:
            return False

    def has_skip_ext(self, url: str) -> bool:
        try:
            path = (urlparse(url).path or "").lower()
        except Exception:
            path = (url or "").lower()
        return any(path.endswith(ext) for ext in self.cfg.SKIP_EXTS)

    def can_fetch(self, url: str) -> bool:
        if not url:
            return False
        if not self.is_same_domain(url):
            return False
        if self.has_skip_ext(url):
            return False
        if not self.is_under_prefix(url):
            return False
        if self.cfg.RESPECT_ROBOTS and self.rp:
            try:
                if not self.rp.can_fetch(self.session.headers["User-Agent"], url):
                    return False
            except Exception:
                pass
        return True

    # ---------- 诊断：入口页探测 ----------
    def probe(self, url: str) -> Dict:
        try:
            self._polite_delay()
            r = self.session.get(url, timeout=self.cfg.REQUEST_TIMEOUT, allow_redirects=True)
            return {
                "ok": True,
                "status": r.status_code,
                "final_url": r.url,
                "ctype": (r.headers.get("Content-Type") or ""),
                "bytes": len(r.content or b""),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- Robots ----------
    def init_robots(self):
        if not self.cfg.RESPECT_ROBOTS:
            self.rp = None
            return
        robots_url = urljoin(self.base_url_full, "/robots.txt")
        rp = RobotFileParser()
        try:
            rp.set_url(robots_url)
            rp.read()
            self.rp = rp
        except Exception:
            self.rp = None

    # ---------- 网络 ----------
    def fetch_response(self, url: str, allow_non_html: bool = False) -> Optional[requests.Response]:
        for attempt in range(self.cfg.RETRY_TIMES + 1):
            try:
                self._polite_delay()
                resp = self.session.get(url, timeout=self.cfg.REQUEST_TIMEOUT, allow_redirects=True)

                if resp.status_code != 200:
                    if resp.status_code == 429:
                        time.sleep(self.cfg.RETRY_SLEEP_BASE * (2 ** attempt) + random.random() * 0.3)
                        continue
                    if 500 <= resp.status_code < 600:
                        time.sleep(self.cfg.RETRY_SLEEP_BASE * (attempt + 1))
                        continue
                    return None

                if not allow_non_html:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if not any(k in ctype for k in self.cfg.ALLOW_CONTENT_TYPE):
                        return None
                    if resp.content and len(resp.content) > self.cfg.MAX_HTML_BYTES:
                        return None

                return resp
            except Exception:
                time.sleep(self.cfg.RETRY_SLEEP_BASE * (attempt + 1))
        return None

    def fetch_text(self, url: str, allow_non_html: bool = False) -> Optional[str]:
        resp = self.fetch_response(url, allow_non_html=allow_non_html)
        if not resp:
            return None
        try:
            resp.encoding = resp.apparent_encoding or resp.encoding
            return resp.text
        except Exception:
            return None

    def fetch_bytes(self, url: str) -> Optional[bytes]:
        resp = self.fetch_response(url, allow_non_html=True)
        if not resp:
            return None
        try:
            return resp.content or b""
        except Exception:
            return None

    def fetch_sitemap_text(self, url: str) -> Optional[str]:
        data = self.fetch_bytes(url)
        if data is None:
            return None
        is_gz = False
        low_url = (url or "").lower()
        if low_url.endswith(".gz"):
            is_gz = True
        if len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B:
            is_gz = True
        if is_gz:
            try:
                data = gzip.decompress(data)
            except Exception:
                pass
        for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
            try:
                txt = data.decode(enc)
                if "<" in txt:
                    return txt
            except Exception:
                continue
        return None

    # ---------- Sitemap ----------
    def parse_sitemap(self, xml_text: str) -> Tuple[List[str], List[str]]:
        soup = make_soup(xml_text, kind="xml")
        urls: List[str] = []
        children: List[str] = []

        for sm in soup.find_all("sitemap"):
            loc = sm.find("loc")
            if loc and loc.get_text(strip=True):
                children.append(loc.get_text(strip=True))

        for u in soup.find_all("url"):
            loc = u.find("loc")
            if loc and loc.get_text(strip=True):
                urls.append(loc.get_text(strip=True))

        if not urls and not children:
            for loc in soup.find_all("loc"):
                t = loc.get_text(strip=True)
                if t:
                    urls.append(t)

        return urls, children

    def get_sitemap_urls(self) -> List[str]:
        self.log_progress(5, "扫描 sitemap...", "scanning")

        candidates = [
            urljoin(self.base_url_full, "/sitemap.xml"),
            urljoin(self.base_url_full, "/sitemap_index.xml"),
            urljoin(self.base_url_full, "/sitemap-index.xml"),
            urljoin(self.base_url_full, "/sitemap.xml.gz"),
            urljoin(self.base_url_full, "/sitemap_index.xml.gz"),
            urljoin(self.base_url_full, "/sitemap-index.xml.gz"),

            # 起始路径下的 sitemap（对 /docs/v2/ 部署很常见）
            urljoin(self.start_url, "sitemap.xml"),
            urljoin(self.start_url, "sitemap.xml.gz"),
            urljoin(self.start_url, "sitemap_index.xml"),
            urljoin(self.start_url, "sitemap_index.xml.gz"),
        ]

        page_urls: Set[str] = set()
        seen_sm: Set[str] = set()

        def add_page(u: str):
            nu = normalize_url(u)
            if nu and self.can_fetch(nu):
                page_urls.add(nu)

        entry = None
        for sm in candidates:
            txt = self.fetch_sitemap_text(sm)
            if txt and "<" in txt:
                entry = sm
                self.log_progress(6, f"找到 sitemap: {sm}", "scanning")
                break

        if not entry:
            self.log_progress(6, "未找到 sitemap", "scanning")
            return []

        q = deque([entry])
        while q and len(seen_sm) < 80 and len(page_urls) < self.cfg.MAX_PAGES_TO_FETCH:
            sm_url = q.popleft()
            if sm_url in seen_sm:
                continue
            seen_sm.add(sm_url)

            xml_text = self.fetch_sitemap_text(sm_url)
            if not xml_text:
                continue

            urls, children = self.parse_sitemap(xml_text)
            for u in urls:
                add_page(u)

            for c in children:
                c_norm = normalize_url(c)
                if c_norm and self.is_same_domain(c_norm):
                    q.append(c_norm)

        out = sorted(page_urls)
        if out:
            self.log_progress(8, f"sitemap 获取页面数: {len(out)}", "scanning")
        return out

    # ---------- MkDocs 搜索索引 ----------
    def try_mkdocs_search_index(self) -> List[str]:
        self.log_progress(6, "尝试 MkDocs search_index.json ...", "scanning")
        html = self.fetch_text(self.start_url, allow_non_html=False)
        if not html:
            self.log_progress(6, "入口页无法获取，MkDocs 索引跳过", "scanning")
            return []
        if "mkdocs" not in html.lower():
            self.log_progress(6, "入口页未检测到 mkdocs 标识，索引跳过", "scanning")
            return []

        idx_url = urljoin(self.start_url, "search/search_index.json")
        raw = self.fetch_text(idx_url, allow_non_html=True)
        if not raw:
            self.log_progress(6, f"索引不可用: {idx_url}", "scanning")
            return []

        try:
            data = json.loads(raw)
        except Exception:
            self.log_progress(6, "索引 JSON 解析失败", "scanning")
            return []

        docs = data.get("docs")
        if not isinstance(docs, list):
            self.log_progress(6, "索引结构异常（缺少 docs）", "scanning")
            return []

        out: Set[str] = set()
        for d in docs:
            loc = (d.get("location") or "").strip()
            if not loc:
                continue
            full = urljoin(self.start_url, loc)
            n = normalize_url(full)
            if n and self.can_fetch(n):
                out.add(n)

        res = sorted(out)
        if res:
            self.log_progress(8, f"MkDocs 索引获取页面数: {len(res)}", "scanning")
        else:
            self.log_progress(8, "MkDocs 索引未产出可抓取页面（可能被 robots/前缀限制过滤）", "scanning")
        return res

    # ---------- BFS ----------
    def bfs_discover_urls(self) -> List[str]:
        self.log_progress(6, "启动 BFS 扫描...", "scanning")
        start_norm = normalize_url(self.start_url)
        if not start_norm:
            return []

        found: Set[str] = set([start_norm])
        results: List[str] = [start_norm]
        q = deque([(start_norm, 0)])
        scanned = 0

        while q and scanned < self.cfg.MAX_SCAN_PAGES and len(results) < self.cfg.MAX_PAGES_TO_FETCH:
            url, depth = q.popleft()
            if depth > self.cfg.MAX_DEPTH:
                continue

            html = self.fetch_text(url, allow_non_html=False)
            if not html:
                continue

            scanned += 1
            if scanned % 10 == 0:
                self.log_progress(6, f"BFS 扫描：已扫 {scanned} 页，发现 {len(results)} URL", "scanning")

            soup = make_soup(html, kind="html")
            # 扫描阶段保留导航（文档站链接主要在导航里），只删脚本样式
            for tag in soup(["script", "style", "noscript", "iframe", "svg", "canvas"]):
                tag.decompose()

            for a in soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                if not href or href.startswith(("mailto:", "javascript:", "tel:")):
                    continue

                full = urljoin(url, href)
                norm = normalize_url(full)
                if not norm:
                    continue
                if norm in found:
                    continue
                if not self.can_fetch(norm):
                    continue

                found.add(norm)
                results.append(norm)
                q.append((norm, depth + 1))

        return results

    # ---------- 处理页面 ----------
    def process_url(self, url: str) -> Optional[Dict]:
        norm = normalize_url(url)
        if not norm:
            return None

        with self.visited_lock:
            if norm in self.visited_normalized:
                return None
            self.visited_normalized.add(norm)

        html = self.fetch_text(norm, allow_non_html=False)
        if not html:
            return None

        soup = make_soup(html, kind="html")

        title = None
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        title = title or norm

        if self.content_selector:
            for tag in soup(["script", "style", "noscript", "iframe", "svg", "canvas"]):
                tag.decompose()
            content = pick_main_content(soup, override_selector=self.content_selector)
            if not content:
                return None
            remove_noise_nodes_local(content)
        else:
            remove_noise_nodes_global(soup)
            content = pick_main_content(soup, override_selector=None)
            if not content:
                return None

        try:
            absolutize_links_in_content(content, norm)
        except Exception:
            pass

        md_text = md(str(content), heading_style="ATX")
        md_text = cleanup_markdown(md_text, self.cfg)

        if not md_text or len(md_text) < self.cfg.MIN_MD_LEN:
            return None

        return {"title": title, "url": norm, "content": md_text}

    def save_item(self, item: Dict):
        with self.file_lock:
            with open(self.output_path, "a", encoding="utf-8") as f:
                f.write("\n---\n\n")
                f.write(f"## {item['title']}\n\n")
                if self.cfg.INCLUDE_SOURCE_LINE:
                    f.write(f"*Source: {item['url']}*\n\n")
                f.write(item["content"].strip() + "\n")

    # ---------- 发现 URL ----------
    def discover_urls(self) -> List[str]:
        # 入口探测（关键诊断）
        p = self.probe(self.start_url)
        if not p.get("ok"):
            self.log_progress(100, f"入口页请求失败：{p.get('error')}", "error")
            return []
        self.log_progress(4, f"入口页状态：{p['status']} | {p['ctype']} | bytes={p['bytes']}", "scanning")
        if p["status"] != 200:
            self.log_progress(100, f"入口页非 200（{p['status']}），无法继续。", "error")
            return []

        # robots
        self.init_robots()
        if self.cfg.RESPECT_ROBOTS and self.rp:
            if not self.rp.can_fetch(self.session.headers["User-Agent"], self.start_url):
                self.log_progress(
                    100,
                    "robots.txt 禁止抓取该路径（建议在 UI 里关闭“尊重 robots.txt”后重试）",
                    "error"
                )
                return []

        # sitemap
        urls = self.get_sitemap_urls()
        if urls:
            return urls[: self.cfg.MAX_PAGES_TO_FETCH]

        # mkdocs search index
        urls = self.try_mkdocs_search_index()
        if urls:
            return urls[: self.cfg.MAX_PAGES_TO_FETCH]

        # bfs
        return self.bfs_discover_urls()[: self.cfg.MAX_PAGES_TO_FETCH]

    def run(self):
        try:
            self.log_progress(1, "任务启动，准备发现页面...", "scanning")
            urls = self.discover_urls()

            # 过滤
            urls = [u for u in urls if u and self.can_fetch(normalize_url(u))]

            # 兜底：至少保留起始页（除非 robots 拦截导致 discover_urls 已返回空并 error）
            start_norm = normalize_url(self.start_url)
            if start_norm and start_norm not in urls and self.is_same_domain(start_norm) and self.is_under_prefix(start_norm):
                urls.insert(0, start_norm)

            total = len(urls)
            if total == 0:
                self.log_progress(100, "未发现有效页面（请看日志：是 robots 拦截还是入口不可达）", "error")
                return

            extra = f"（正文选择器: {self.content_selector}）" if self.content_selector else ""
            self.log_progress(10, f"发现 {total} 个页面，开始抓取...{extra}", "processing")

            completed = 0
            saved = 0

            with ThreadPoolExecutor(max_workers=self.cfg.MAX_WORKERS) as executor:
                futures = {executor.submit(self.process_url, u): u for u in urls}
                for future in as_completed(futures):
                    u = futures[future]
                    try:
                        res = future.result()
                        if res:
                            self.save_item(res)
                            saved += 1
                    except Exception as e:
                        logger.error(f"处理失败: {u} | {e}")

                    completed += 1
                    percent = 10 + int((completed / total) * 85)
                    tail = (u.split("/")[-2] if u.endswith("/") else u.split("/")[-1] or "/")[:28]
                    self.log_progress(
                        percent,
                        f"进度 [{completed}/{total}] : {tail} ...（已写入 {saved} 页）",
                        "processing"
                    )

            self.log_progress(100, f"抓取完成！共写入 {saved} 页，准备下载。", "completed")

        except Exception as e:
            logger.error(f"Critical Error: {e}")
            self.log_progress(100, f"发生错误: {str(e)}", "error")


# -----------------------------
# Flask Web 应用
# -----------------------------
app = Flask(__name__)
task_queues: Dict[str, Queue] = {}
task_files: Dict[str, str] = {}
task_started_at: Dict[str, float] = {}
task_loggers: Dict[str, TaskLogger] = {}

def is_valid_start_url(u: str) -> bool:
    try:
        p = urlparse(u.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

def sanitize_selector(selector: Optional[str], cfg: CrawlerConfig) -> Optional[str]:
    if not selector:
        return None
    selector = selector.strip()
    if not selector:
        return None
    if len(selector) > cfg.MAX_SELECTOR_LEN:
        return None
    if re.search(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", selector):
        return None
    if selector.count("{") or selector.count("}"):
        return None
    return selector

def cleanup_expired_files():
    while True:
        try:
            now = time.time()
            expired = []
            for task_id, ts in list(task_started_at.items()):
                if now - ts > CFG.FILE_TTL_SECONDS:
                    expired.append(task_id)

            for task_id in expired:
                path = task_files.get(task_id)
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                # 清任务
                task_queues.pop(task_id, None)
                task_files.pop(task_id, None)
                task_started_at.pop(task_id, None)
                task_loggers.pop(task_id, None)

            time.sleep(15)
        except Exception:
            time.sleep(15)

_cleaner_thread = threading.Thread(target=cleanup_expired_files, daemon=True)
_cleaner_thread.start()

@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/assets/<path:filename>")
def frontend_assets(filename: str):
    return send_from_directory(FRONTEND_DIR, filename)

@app.route("/api/start", methods=["POST"])
def start_crawl():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not is_valid_start_url(url):
        return jsonify({"error": "请输入有效的 http/https URL"}), 400

    # 允许 UI 覆盖两项关键开关
    respect_robots = bool(data.get("respect_robots", True))
    restrict_prefix = bool(data.get("restrict_prefix", True))

    cfg = replace(CFG,
                  RESPECT_ROBOTS=respect_robots,
                  RESTRICT_TO_START_PATH_PREFIX=restrict_prefix)

    selector = sanitize_selector(data.get("selector"), cfg)

    task_id = str(uuid.uuid4())
    q = Queue()
    task_queues[task_id] = q
    task_started_at[task_id] = time.time()

    log_path = os.path.join(cfg.TEMP_DIR, f"task_{task_id}.log")
    tlog = TaskLogger(log_path)
    task_loggers[task_id] = tlog

    def background_worker(t_id: str, u: str, q_obj: Queue, sel: Optional[str], cfg_obj: CrawlerConfig, tl: TaskLogger):
        crawler = WebCrawler(u, t_id, q_obj, cfg_obj, tl, content_selector=sel)
        crawler.run()
        task_files[t_id] = crawler.output_path

    thread = threading.Thread(target=background_worker, args=(task_id, url, q, selector, cfg, tlog), daemon=True)
    thread.start()

    return jsonify({"status": "started", "task_id": task_id})

@app.route("/api/stream/<task_id>")
def stream_progress(task_id: str):
    def generate():
        q = task_queues.get(task_id)
        if not q:
            yield f"data: {json.dumps({'percent':100,'message':'任务不存在或已过期','status':'error'}, ensure_ascii=False)}\n\n"
            return

        while True:
            try:
                data = q.get(timeout=15)
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                if data.get("status") in ("completed", "error"):
                    break
            except Empty:
                yield ": ping\n\n"
            except GeneratorExit:
                break
            except Exception:
                break

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers=headers)

@app.route("/api/download/<task_id>")
def download_file(task_id: str):
    file_path = task_files.get(task_id) or os.path.join(CFG.TEMP_DIR, f"官方文档_{task_id}.md")
    if not os.path.exists(file_path):
        return "File not found or expired", 404

    @after_this_request
    def remove_file(response):
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
            task_queues.pop(task_id, None)
            task_files.pop(task_id, None)
            task_started_at.pop(task_id, None)
            task_loggers.pop(task_id, None)
            logger.info(f"File deleted: {file_path}")
        except Exception as e:
            logger.error(f"Error removing file: {e}")
        return response

    return send_file(
        file_path,
        as_attachment=True,
        download_name="官方文档_全集.md",
        mimetype="text/markdown"
    )

if __name__ == "__main__":
    print("🌐 服务启动中: http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
