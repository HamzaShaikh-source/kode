"""Competition context: files, links and notes the lead attaches so the planner and every agent know what the event is about.

Text is extracted on the Brain (PDF, HTML pages, plain text) and stored with the project; the original files are kept under project_data/<id>/context/
and copied to every teammate's working folder as .kode/context/ so agents can open images and documents themselves.
Links are fetched by the Brain only after a private-address check (no localhost, LAN or cloud-metadata targets).
"""
import asyncio
import base64
import html
import io
import ipaddress
import re
import secrets
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

DATA = Path(__file__).resolve().parents[1] / "project_data"
MAX_FILE = 6_000_000
MAX_ITEMS = 14
MAX_TEXT = 12_000                  # extracted characters kept per item
MAX_PLANNER = 9_000                # characters of context shown to the planner in total
TEXT_EXT = {".txt", ".md", ".markdown", ".json", ".csv", ".html", ".htm", ".yaml", ".yml", ".xml", ".py", ".js", ".ts", ".css", ".toml", ".rst", ".log"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


class ContextError(Exception):
    pass


def storage(pid: str) -> Path:
    d = DATA / re.sub(r"[^A-Za-z0-9_-]", "", pid) / "context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(str(name)).name).strip(" .")[:80]
    return n or "file"


def extract_text(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            r = PdfReader(io.BytesIO(data))
            return "\n".join((pg.extract_text() or "") for pg in r.pages[:40]).strip()[:MAX_TEXT]
        except Exception:
            return ""
    if ext in (".html", ".htm"):
        return html_to_text(data.decode("utf-8", "replace"))[:MAX_TEXT]
    if ext in TEXT_EXT:
        return data.decode("utf-8", "replace")[:MAX_TEXT]
    return ""


def html_to_text(src: str) -> str:
    src = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", src)
    src = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", src)
    src = re.sub(r"<[^>]+>", " ", src)
    src = html.unescape(src)
    src = re.sub(r"[ \t\r\f\v]+", " ", src)
    return re.sub(r"\n\s*\n+", "\n\n", src).strip()


def check_public_url(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ContextError("only http and https links can be added")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        raise ContextError(f"could not resolve {u.hostname}")
    for fam, _, _, _, sa in infos:
        ip = ipaddress.ip_address(sa[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ContextError("links to this computer or a private network are not fetched")
    return u.geturl()


async def fetch_link(url: str) -> Dict[str, str]:
    import httpx
    cur = check_public_url(url)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0 K.O.D.E. context fetcher"}) as c:
        for _ in range(5):
            r = await c.get(cur)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                nxt = str(httpx.URL(cur).join(r.headers["location"]))
                cur = check_public_url(nxt)                                   # every redirect target is checked too
                continue
            break
        else:
            raise ContextError("too many redirects")
        if r.status_code >= 400:
            raise ContextError(f"the page answered {r.status_code}")
        ctype = r.headers.get("content-type", "")
        body = r.content[:1_500_000]
        if "pdf" in ctype or cur.lower().endswith(".pdf"):
            return {"title": Path(urlparse(cur).path).name or cur, "text": extract_text("x.pdf", body)}
        text = body.decode(r.encoding or "utf-8", "replace")
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        return {"title": html.unescape(m.group(1)).strip()[:120] if m else cur, "text": html_to_text(text)[:MAX_TEXT] if "html" in ctype or "<html" in text[:500].lower() else text[:MAX_TEXT]}


def add_file(p: dict, name: str, mime: str, b64: str) -> dict:
    items = p.setdefault("context", [])
    if len(items) >= MAX_ITEMS:
        raise ContextError(f"at most {MAX_ITEMS} context items per project")
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception:
        raise ContextError("the file could not be read")
    if len(data) > MAX_FILE:
        raise ContextError(f"{name}: files can be at most {MAX_FILE // 1_000_000} MB")
    fname = safe_name(name)
    iid = "x_" + secrets.token_hex(3)
    (storage(p["id"]) / f"{iid}-{fname}").write_bytes(data)
    ext = Path(fname).suffix.lower()
    item = {"id": iid, "kind": "image" if ext in IMAGE_EXT else "file", "name": fname, "mime": str(mime)[:60], "size": len(data), "text": extract_text(fname, data), "file": f"{iid}-{fname}", "added": int(__import__("time").time())}
    items.append(item)
    return item


async def add_link(p: dict, url: str) -> dict:
    items = p.setdefault("context", [])
    if len(items) >= MAX_ITEMS:
        raise ContextError(f"at most {MAX_ITEMS} context items per project")
    page = await fetch_link(url)
    item = {"id": "x_" + secrets.token_hex(3), "kind": "link", "name": page["title"] or url, "url": url.strip()[:500], "size": len(page["text"]), "text": page["text"], "file": "", "added": int(__import__("time").time())}
    items.append(item)
    return item


def add_note(p: dict, text: str, name: str = "Note") -> dict:
    items = p.setdefault("context", [])
    if len(items) >= MAX_ITEMS:
        raise ContextError(f"at most {MAX_ITEMS} context items per project")
    item = {"id": "x_" + secrets.token_hex(3), "kind": "note", "name": name[:60] or "Note", "size": len(text), "text": text.strip()[:MAX_TEXT], "file": "", "added": int(__import__("time").time())}
    items.append(item)
    return item


def remove(p: dict, iid: str) -> None:
    it = next((x for x in p.get("context", []) if x["id"] == iid), None)
    if it:
        if it.get("file"):
            try:
                (storage(p["id"]) / it["file"]).unlink()
            except OSError:
                pass
        p["context"] = [x for x in p["context"] if x["id"] != iid]


def summary(p: dict, limit: int = MAX_PLANNER) -> str:
    """What the planner and the agents are told about the attached material."""
    items = p.get("context") or []
    if not items:
        return ""
    per = max(600, limit // max(1, len(items)))
    out = []
    for it in items:
        head = f"- [{it['kind']}] {it['name']}" + (f" ({it['url']})" if it.get("url") else "")
        if it.get("text"):
            out.append(head + "\n" + chr(10).join("    " + ln for ln in it["text"][:per].splitlines() if ln.strip()))
        else:
            out.append(head + ("  (image: open it in .kode/context/ to look at it)" if it["kind"] == "image" else "  (no text could be extracted; the original is in .kode/context/)"))
    return chr(10).join(out)[:limit + 2000]


def files_payload(p: dict) -> Dict[str, str]:
    """name -> base64 for every stored original, plus an INDEX.md, for copying into a teammate's .kode/context/."""
    out: Dict[str, str] = {}
    idx = ["# Competition context", ""]
    for it in p.get("context") or []:
        idx.append(f"- {it['name']} ({it['kind']})" + (f": {it['url']}" if it.get("url") else ""))
        if it.get("file"):
            f = storage(p["id"]) / it["file"]
            if f.is_file():
                out[it["file"]] = base64.b64encode(f.read_bytes()).decode("ascii")
        elif it.get("text"):
            out[f"{it['id']}-{safe_name(it['name'])}.txt"] = base64.b64encode(it["text"].encode("utf-8")).decode("ascii")
    out["INDEX.md"] = base64.b64encode(chr(10).join(idx).encode("utf-8")).decode("ascii")
    return out


def public(p: dict) -> List[dict]:
    return [{k: v for k, v in it.items() if k != "text"} | {"chars": len(it.get("text", ""))} for it in p.get("context") or []]
