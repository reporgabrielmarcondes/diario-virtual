#!/usr/bin/env python3
"""Servidor local do O Diario Virtual.

Serve a pagina e faz as buscas no lado do servidor (sem CORS, sem proxies
publicos). Tambem extrai imagem e texto das materias para o modo de leitura.

Uso:  python servidor.py      (depois abra http://localhost:8000)
Chaves de APIs de noticias (opcionais): cole na pagina ou defina variaveis de
ambiente NEWSAPI_KEY, CURRENTS_API_KEY, GNEWS_KEY, NEWSDATA_KEY, THENEWSAPI_KEY.
Cada variavel aceita varias chaves separadas por virgula: ao esgotar uma, usa a proxima.
Nuvem (opcional): SUPABASE_URL e SUPABASE_ANON_KEY (chave publica) habilitam login e sincronizacao.
"""
import base64
import hashlib
import html as htmlmod
import ipaddress
import json
import os
import re
import socket
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "jornal-virtual.html")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 6 * 3600
_cache = {}


def fetch(url, headers=None, data=None, timeout=20, limit=None):
    h = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit) if limit else r.read(), \
                r.headers.get("Content-Type", "text/plain"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "text/plain"), url


def decode_bytes(body, ctype=""):
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I) or \
        re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    enc = (m.group(1).decode() if m and isinstance(m.group(1), bytes) else
           (m.group(1) if m else "utf-8"))
    try:
        return body.decode(enc, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def public_http(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except OSError:
        return False
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


# ---------- Links do Google Noticias -> link real ----------
def decode_b64(gid):
    try:
        b = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
    except Exception:
        return None
    if b.startswith(b"\x08\x13\x22"):
        b = b[3:]
    if b.endswith(b"\xd2\x01\x00"):
        b = b[:-3]
    if not b:
        return None
    n, off = b[0], 1
    if n & 0x80 and len(b) > 1:
        n, off = (n & 0x7F) | (b[1] << 7), 2
    s = b[off:off + n].decode("utf-8", "ignore")
    return s if s.startswith("http") else None


def resolve_google(url):
    m = re.search(r"articles/([^/?]+)", urllib.parse.urlparse(url).path)
    if not m:
        raise ValueError("link do Google Noticias invalido")
    gid = m.group(1)
    direct = decode_b64(gid)
    if direct:
        return direct
    sg = ts = None
    diag = []
    for page in (f"https://news.google.com/articles/{gid}?hl=pt-BR&gl=BR&ceid=BR:pt-419",
                 f"https://news.google.com/rss/articles/{gid}?hl=pt-BR&gl=BR&ceid=BR:pt-419"):
        code, body, ctype, _ = fetch(page, headers={"Cookie": "CONSENT=YES+cb; SOCS=CAI"})
        html = decode_bytes(body, ctype)
        sg = re.search(r'data-n-a-sg="([^"]+)"', html)
        ts = re.search(r'data-n-a-ts="([^"]+)"', html)
        diag.append(f"HTTP {code}" + ("" if (sg and ts) else ", sem dados"))
        if sg and ts:
            break
    if not sg or not ts:
        raise ValueError("nao foi possivel resolver o link do Google Noticias (" + "; ".join(diag) + ")")
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,'
             'null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
             f'"{gid}",{ts.group(1)},"{sg.group(1)}"]')
    payload = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner]]]))
    code, body, ctype, _ = fetch(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data=payload.encode())
    try:
        parsed = json.loads(body.decode("utf-8", "replace").split("\n\n")[1])[:-2]
        return json.loads(parsed[0][2])[1]
    except Exception:
        raise ValueError(f"nao foi possivel resolver o link do Google Noticias (batchexecute HTTP {code})")


# ---------- Extracao de imagem e texto ----------
class Extractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "footer", "aside", "form",
            "header", "svg", "iframe", "button", "figcaption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.title = {}, ""
        self.skip = self.art = 0
        self.in_p = self.in_title = False
        self.buf, self.p_all, self.p_art = [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            v = a.get("content")
            if k and v and k not in self.meta:
                self.meta[k] = v
        elif tag == "title":
            self.in_title = True
        elif tag in self.SKIP:
            self.skip += 1
        elif tag == "article":
            self.art += 1
        elif tag == "p" and not self.skip:
            self.in_p, self.buf = True, []
        elif tag == "br" and self.in_p:
            self.buf.append(" ")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "article" and self.art:
            self.art -= 1
        elif tag == "p" and self.in_p:
            self.in_p = False
            t = " ".join("".join(self.buf).split())
            if t:
                self.p_all.append(t)
                if self.art:
                    self.p_art.append(t)

    def handle_data(self, d):
        if self.in_title:
            self.title += d
        if self.in_p and not self.skip:
            self.buf.append(d)


def extract(html, base_url):
    ex = Extractor()
    try:
        ex.feed(html)
    except Exception:
        pass
    m = ex.meta
    good = lambda ps: [p for p in ps if len(p) >= 40]
    paras = good(ex.p_art) if len(good(ex.p_art)) >= 2 else good(ex.p_all)
    out, seen, total = [], set(), 0
    for p in paras:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        total += len(p)
        if len(out) >= 60 or total > 20000:
            break
    img = m.get("og:image") or m.get("twitter:image") or m.get("twitter:image:src") or ""
    if img:
        img = urllib.parse.urljoin(base_url, img)
        if not img.startswith("http"):
            img = ""
    return {
        "url": base_url,
        "title": (m.get("og:title") or ex.title).strip(),
        "site": m.get("og:site_name") or urllib.parse.urlparse(base_url).hostname or "",
        "description": (m.get("og:description") or m.get("description") or "").strip(),
        "image": img,
        "paragraphs": out,
    }


def get_article(url):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    real = url
    if urllib.parse.urlparse(url).hostname == "news.google.com":
        real = resolve_google(url)
    if not public_http(real):
        raise ValueError("endereco nao permitido")
    code, body, ctype, final = fetch(real, headers={"Accept": "text/html"}, limit=2_500_000)
    if code >= 400:
        raise ValueError(f"site respondeu HTTP {code}")
    data = extract(decode_bytes(body, ctype), final)
    _cache[url] = (time.time(), data)
    return data


# ---------- Feeds RSS dos portais (sem passar pelo Google) ----------
# dominio -> (nome, [feed geral], {editoria: feed da editoria})
# Se algum feed sair do ar, o servidor tenta descobrir outro na pagina inicial do portal.
FEEDS = {
    "g1.globo.com": ("G1", ["https://g1.globo.com/rss/g1/"], {
        "economia": "https://g1.globo.com/rss/g1/economia/",
        "tecnologia": "https://g1.globo.com/rss/g1/tecnologia/",
        "mundo": "https://g1.globo.com/rss/g1/mundo/",
        "politica": "https://g1.globo.com/rss/g1/politica/",
        "ciencia": "https://g1.globo.com/rss/g1/ciencia-e-saude/",
        "saude": "https://g1.globo.com/rss/g1/ciencia-e-saude/",
    }),
    "folha.uol.com.br": ("Folha de S.Paulo", ["https://feeds.folha.uol.com.br/emcimadahora/rss091.xml"], {
        "economia": "https://feeds.folha.uol.com.br/mercado/rss091.xml",
        "tecnologia": "https://feeds.folha.uol.com.br/tec/rss091.xml",
        "mundo": "https://feeds.folha.uol.com.br/mundo/rss091.xml",
        "politica": "https://feeds.folha.uol.com.br/poder/rss091.xml",
        "esporte": "https://feeds.folha.uol.com.br/esporte/rss091.xml",
        "ciencia": "https://feeds.folha.uol.com.br/ciencia/rss091.xml",
        "saude": "https://feeds.folha.uol.com.br/equilibrioesaude/rss091.xml",
        "cultura": "https://feeds.folha.uol.com.br/ilustrada/rss091.xml",
    }),
    "estadao.com.br": ("O Estado de S. Paulo",
                       ["https://www.estadao.com.br/arc/outboundfeeds/rss/?outputType=xml"], {}),
    "cnnbrasil.com.br": ("CNN Brasil", ["https://www.cnnbrasil.com.br/feed/"], {
        "economia": "https://www.cnnbrasil.com.br/economia/feed/",
        "tecnologia": "https://www.cnnbrasil.com.br/tecnologia/feed/",
        "mundo": "https://www.cnnbrasil.com.br/internacional/feed/",
        "politica": "https://www.cnnbrasil.com.br/politica/feed/",
        "esporte": "https://www.cnnbrasil.com.br/esportes/feed/",
        "saude": "https://www.cnnbrasil.com.br/saude/feed/",
    }),
    "bbc.com": ("BBC News Brasil", ["https://feeds.bbci.co.uk/portuguese/rss.xml"], {}),
    "uol.com.br": ("UOL", ["https://rss.uol.com.br/feed/noticias.xml"], {
        "economia": "https://rss.uol.com.br/feed/economia.xml",
        "tecnologia": "https://rss.uol.com.br/feed/tecnologia.xml",
    }),
    "agenciabrasil.ebc.com.br": ("Agencia Brasil",
                                 ["https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml"], {}),
    "poder360.com.br": ("Poder360", ["https://www.poder360.com.br/feed/"], {}),
    "infomoney.com.br": ("InfoMoney", ["https://www.infomoney.com.br/feed/"], {}),
    "exame.com": ("Exame", ["https://exame.com/feed/"], {}),
    "tecnoblog.net": ("Tecnoblog", ["https://tecnoblog.net/feed/"], {}),
    "metropoles.com": ("Metropoles", ["https://www.metropoles.com/feed"], {}),
}
DEFAULT_DOMAINS = ["g1.globo.com", "folha.uol.com.br", "estadao.com.br",
                   "cnnbrasil.com.br", "bbc.com", "noticias.uol.com.br"]
# trecho do topico -> editoria
SECTION_STEMS = {
    "economia": "economia", "mercado": "economia", "negocio": "economia", "financ": "economia",
    "tecnolog": "tecnologia", "tech": "tecnologia",
    "mundo": "mundo", "internacional": "mundo",
    "politic": "politica", "esporte": "esporte", "futebol": "esporte",
    "cienc": "ciencia", "saude": "saude", "cultura": "cultura", "entretenimento": "cultura",
}
GENERIC = {"brasil", "geral", "noticias", "ultimas noticias", "principais", "destaques",
           "manchetes", "hoje", "ultimas", "em alta", "ultimas noticias do brasil"}
STOP = {"das", "dos", "para", "com", "que", "uma", "por", "sobre", "the", "and"}
FEED_TTL = 300
WINDOW = 72 * 3600
_feed_cache, _disc_cache = {}, {}
_locks = defaultdict(threading.Lock)


def norm(s):
    s = unicodedata.normalize("NFD", s or "").lower()
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def lookup(dom):
    best = None
    for k in FEEDS:
        if dom == k or dom.endswith("." + k):
            if best is None or len(k) > len(best):
                best = k
    return FEEDS[best] if best else None


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _text(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def _plain(html_text):
    return " ".join(re.sub(r"<[^>]+>", " ", htmlmod.unescape(html_text or "")).split())


def _date(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        d = parsedate_to_datetime(v)
    except Exception:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def parse_feed(body, base, name, dom):
    body = body.lstrip()
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", decode_bytes(body))
        txt = re.sub(r"^<\?xml[^>]*\?>", "", txt.lstrip())
        root = ET.fromstring(txt)
    now = time.time()
    out = []
    for e in root.iter():
        if _local(e.tag) not in ("item", "entry"):
            continue
        title = link = desc = ""
        ts = None
        cats, imgs, htmls = [], [], []
        for c in e:
            t = _local(c.tag)
            if t == "title":
                title = _plain(_text(c))
            elif t == "link":
                if c.get("href"):
                    if c.get("rel") in (None, "alternate"):
                        link = link or c.get("href")
                else:
                    link = _text(c) or link
            elif t in ("origlink",) and _text(c):
                link = _text(c)
            elif t == "guid" and not link and _text(c).startswith("http"):
                link = _text(c)
            elif t in ("pubdate", "published", "date", "updated"):
                ts = ts or _date(_text(c))
            elif t in ("description", "summary", "subtitle"):
                htmls.append(_text(c))
                desc = desc or _plain(_text(c))
            elif t in ("encoded", "content") and not c.get("url"):
                htmls.append(_text(c))
            elif t == "category":
                cats.append(_text(c) or c.get("term") or "")
        for el in e.iter():
            t = _local(el.tag)
            if t in ("content", "thumbnail") and el.get("url"):
                kind = (el.get("type") or el.get("medium") or "image").lower()
                if "image" in kind:
                    imgs.append(el.get("url"))
            elif t == "enclosure" and (el.get("type") or "").startswith("image") and el.get("url"):
                imgs.append(el.get("url"))
        if not imgs:
            for h in htmls:
                m = re.search(r"<img[^>]+src=[\"']([^\"']+)", h, re.I)
                if m:
                    imgs.append(m.group(1))
                    break
        link = urllib.parse.urljoin(base, link) if link else ""
        if not title or not link.startswith("http"):
            continue
        img = urllib.parse.urljoin(base, htmlmod.unescape(imgs[0])) if imgs else ""
        if not img.startswith("http"):
            img = ""
        ts = ts or now
        if ts < now - WINDOW:
            continue
        out.append({"title": title, "link": link, "ts": ts, "image": img, "source": name,
                    "domain": dom, "hay": norm(title + " " + desc + " " + " ".join(cats))})
    return out


def get_feed(url, name, dom):
    with _locks[url]:
        hit = _feed_cache.get(url)
        if hit and time.time() - hit[0] < FEED_TTL:
            return hit[1]
        code, body, ctype, final = fetch(url, headers={
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
            timeout=12, limit=3_000_000)
        if code >= 400:
            raise ValueError(f"HTTP {code}")
        items = parse_feed(body, final, name, dom)
        _feed_cache[url] = (time.time(), items)
        return items


def discover(dom):
    hit = _disc_cache.get(dom)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    urls = []
    base = f"https://{dom}/"
    if public_http(base):
        try:
            code, body, ctype, final = fetch(base, headers={"Accept": "text/html"}, timeout=12, limit=400_000)
            page = decode_bytes(body, ctype)
            for m in re.finditer(r"<link\b[^>]*>", page, re.I):
                tag = m.group(0)
                if re.search(r"type=[\"']application/(rss|atom)\+xml", tag, re.I):
                    h = re.search(r"href=[\"']([^\"']+)", tag, re.I)
                    if h:
                        urls.append(urllib.parse.urljoin(final, htmlmod.unescape(h.group(1))))
        except Exception:
            pass
        urls = urls[:2] + [base + "feed/", base + "rss", base + "feed", base + "rss.xml"]
        urls = [u for i, u in enumerate(urls) if u not in urls[:i] and public_http(u)]
    _disc_cache[dom] = (time.time(), urls)
    return urls


def _run_job(job):
    name, dom, urls, filt, tokens = job
    st = {"name": name, "domain": dom, "ok": False, "count": 0, "error": ""}
    items, errs = [], []

    def attempt(cands):
        for u in cands:
            try:
                items.extend(get_feed(u, name, dom))
                return True
            except Exception as ex:  # noqa: BLE001
                errs.append(f"{type(ex).__name__}: {ex}"[:140])
        return False

    if not attempt(urls):
        attempt([u for u in discover(dom) if u not in urls])
    if items:
        st["ok"] = True
    else:
        st["error"] = errs[-1] if errs else "nenhum feed encontrado"
    if filt:
        items = [i for i in items if all(t in i["hay"] for t in tokens)]
    st["count"] = len(items)
    return items, st


def collect(topic, domains):
    tn = norm(topic).strip()
    tokens = [t for t in re.findall(r"[a-z0-9]+", tn) if len(t) >= 3 and t not in STOP]
    generic = tn in GENERIC or not tokens
    section = next((v for k, v in SECTION_STEMS.items() if k in tn), None)
    jobs = []
    for dom in domains:
        info = lookup(dom)
        name, general, secs = info if info else (dom, [], {})
        if generic:
            jobs.append((name, dom, general, False, tokens))
        elif section and section in secs:
            jobs.append((name, dom, [secs[section]], False, tokens))
        else:
            jobs.append((name, dom, general, True, tokens))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_run_job, jobs))
    seen, merged, sources = set(), [], []
    for items, st in results:
        sources.append(st)
        for i in items:
            k = (i["link"], i["title"].lower())
            if k[0] in seen or k[1] in seen:
                continue
            seen.add(k[0]); seen.add(k[1])
            merged.append(i)
    merged.sort(key=lambda i: i["ts"], reverse=True)
    out = [{"title": i["title"], "link": i["link"], "image": i["image"], "source": i["source"],
            "domain": i["domain"],
            "date": datetime.fromtimestamp(i["ts"], timezone.utc).isoformat()} for i in merged[:40]]
    return {"items": out, "sources": sources}


# ---------- APIs de noticias (varios provedores, resposta normalizada) ----------
API_TTL = int(os.environ.get("API_CACHE_TTL", "3600"))   # cache por (provedor, assunto)
COOLDOWN = 1800                                          # pausa de uma chave sem cota
BAD_KEY_TTL = 6 * 3600                                   # pausa de uma chave recusada (401/403)
_api_cache, _cooldown = {}, {}


class ApiError(Exception):
    def __init__(self, msg, code=502):
        super().__init__(msg)
        self.code = code


def api_ts(v):
    """Converte datas ISO / 'YYYY-MM-DD HH:MM:SS +0000' em timestamp UTC."""
    v = str(v or "").strip()
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?", v)
    if m:
        y, mo, d, h, mi, sec = map(int, m.groups()[:6])
        ts = datetime(y, mo, d, h, mi, sec, tzinfo=timezone.utc).timestamp()
        tz = m.group(7)
        if tz and tz != "Z":
            digits = tz[1:].replace(":", "")
            off = int(digits[:2]) * 3600 + int(digits[2:]) * 60
            ts -= -off if tz[0] == "-" else off
        return ts
    return _date(v) or time.time()


def _img(v):
    v = v if isinstance(v, str) else ""
    return v if v.startswith("http") else ""


def _p_currents(j):
    return [{"title": n.get("title"), "link": n.get("url"), "ts": api_ts(n.get("published")),
             "image": _img(n.get("image")), "source": ""} for n in j.get("news") or []]


def _p_newsapi(j):
    return [{"title": n.get("title"), "link": n.get("url"), "ts": api_ts(n.get("publishedAt")),
             "image": _img(n.get("urlToImage")), "source": (n.get("source") or {}).get("name") or ""}
            for n in j.get("articles") or []]


def _p_gnews(j):
    return [{"title": n.get("title"), "link": n.get("url"), "ts": api_ts(n.get("publishedAt")),
             "image": _img(n.get("image")), "source": (n.get("source") or {}).get("name") or ""}
            for n in j.get("articles") or []]


def _p_newsdata(j):
    return [{"title": n.get("title"), "link": n.get("link"), "ts": api_ts(n.get("pubDate")),
             "image": _img(n.get("image_url")), "source": n.get("source_name") or ""}
            for n in j.get("results") or []]


def _p_thenewsapi(j):
    return [{"title": n.get("title"), "link": n.get("url"), "ts": api_ts(n.get("published_at")),
             "image": _img(n.get("image_url")), "source": ""} for n in j.get("data") or []]


def _q(**kw):
    return urllib.parse.urlencode({k: v for k, v in kw.items() if v not in (None, "")})


# id -> (nome, variavel de ambiente, montador de requisicao(topic, key, domains) -> (url, headers), parser)
PROVIDERS = {
    "newsapi": ("NewsAPI", "NEWSAPI_KEY",
                lambda t, k, d: ("https://newsapi.org/v2/everything?" + _q(
                    q=t, language="pt", sortBy="publishedAt", pageSize=40, domains=d),
                    {"X-Api-Key": k}),
                _p_newsapi),
    "currents": ("Currents API", "CURRENTS_API_KEY",
                 lambda t, k, d: ("https://api.currentsapi.services/v1/search?" + _q(
                     keywords=t, language="pt", apiKey=k), {}),
                 _p_currents),
    "gnews": ("GNews", "GNEWS_KEY",
              lambda t, k, d: ("https://gnews.io/api/v4/search?" + _q(
                  q=t, lang="pt", country="br", max=10, sortby="publishedAt", apikey=k), {}),
              _p_gnews),
    "newsdata": ("NewsData.io", "NEWSDATA_KEY",
                 lambda t, k, d: ("https://newsdata.io/api/1/latest?" + _q(
                     q=t, language="pt", apikey=k), {}),
                 _p_newsdata),
    "thenewsapi": ("The News API", "THENEWSAPI_KEY",
                   lambda t, k, d: ("https://api.thenewsapi.com/v1/news/all?" + _q(
                       search=t, language="pt", sort="published_at", api_token=k), {}),
                   _p_thenewsapi),
}


def env_keys(pid):
    return [k.strip() for k in os.environ.get(PROVIDERS[pid][1], "").split(",") if k.strip()]


def _api_call(pid, topic, key, domains):
    """Uma chamada ao provedor com UMA chave. Levanta ApiError(429) se a cota acabou,
    ApiError(401) se a chave foi recusada."""
    name, _, build, parse = PROVIDERS[pid]
    url, headers = build(topic, key, domains)
    code, body, ctype, _ = fetch(url, headers=headers, timeout=15, limit=3_000_000)
    try:
        j = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        j = {}
    if not isinstance(j, dict):
        j = {}
    msg = j.get("message") or j.get("msg") or j.get("errors") or j.get("error") or ""
    if isinstance(msg, dict):
        msg = msg.get("message") or json.dumps(msg, ensure_ascii=False)
    if isinstance(msg, list):
        msg = "; ".join(map(str, msg))
    msg = str(msg)[:200]
    failed = code >= 400 or j.get("status") == "error"
    if failed:
        quota = (code in (429, 402) or "ratelimit" in str(j.get("code", "")).lower()
                 or "usage_limit" in str(j.get("code", "")).lower()
                 or (code == 403 and "limit" in msg.lower()))
        if quota:
            raise ApiError("cota esgotada", 429)
        raise ApiError(f"{name} HTTP {code}" + (f": {msg}" if msg else ""),
                       401 if code in (401, 403) else 502)
    items, seen = [], set()
    for a in parse(j):
        title = " ".join((a.get("title") or "").split())
        link = a.get("link") or ""
        if not title or not link.startswith("http") or title == "[Removed]" or link in seen:
            continue
        seen.add(link)
        dom = (urllib.parse.urlparse(link).hostname or "").removeprefix("www.")
        items.append({"title": title, "link": link, "image": a.get("image") or "",
                      "source": a.get("source") or dom, "domain": dom,
                      "date": datetime.fromtimestamp(a["ts"], timezone.utc).isoformat()})
    items.sort(key=lambda i: i["date"], reverse=True)
    return {"items": items}


def api_news(pid, topic, client_keys, domains=""):
    """Tenta as chaves do provedor em ordem (as do navegador e depois as do servidor).
    Chave sem cota ou recusada entra em pausa e a proxima assume."""
    if pid not in PROVIDERS:
        raise ApiError("provedor desconhecido", 400)
    keys = []
    for k in list(client_keys) + env_keys(pid):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    if not keys:
        raise ApiError("sem chave", 401)
    if not topic:
        raise ApiError("assunto vazio", 400)
    now = time.time()
    ck = (pid, norm(topic), domains)
    hit = _api_cache.get(ck)
    if hit and now - hit[0] < API_TTL:
        return {**hit[1], "keys": len(keys)}
    quota, last = False, None
    for idx, key in enumerate(keys):
        kid = (pid, hashlib.sha1(key.encode()).hexdigest()[:16])
        until, kind, kmsg = _cooldown.get(kid, (0, "", ""))
        if until > now:
            if kind == "quota":
                quota = True
            else:
                last = ApiError(kmsg, 401)
            continue
        try:
            out = _api_call(pid, topic, key, domains)
        except ApiError as e:
            if e.code == 429:
                _cooldown[kid] = (now + COOLDOWN, "quota", "")
                quota = True
                continue
            if e.code == 401:
                _cooldown[kid] = (now + BAD_KEY_TTL, "bad", f"chave {idx + 1}: {e}")
                last = ApiError(f"chave {idx + 1}: {e}", 401)
                continue
            raise
        out["key"] = idx + 1
        _api_cache[ck] = (now, out)
        return {**out, "keys": len(keys)}
    if quota:
        raise ApiError("cota esgotada em todas as chaves" if len(keys) > 1 else "cota esgotada", 429)
    raise last or ApiError("nenhuma chave utilizavel", 401)


def provider_list():
    return [{"id": pid, "name": v[0], "env": len(env_keys(pid))} for pid, v in PROVIDERS.items()]


def _is_secret_key(k):
    """Recusa chaves privadas do Supabase: so a chave publica (anon/publishable) pode ir ao navegador."""
    if k.startswith("sb_secret_"):
        return True
    try:
        part = k.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return payload.get("role") == "service_role"
    except Exception:
        return False


def supabase_config():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if key and _is_secret_key(key):
        return {"supabaseUrl": "", "supabaseKey": "",
                "error": "SUPABASE_ANON_KEY contem uma chave PRIVADA (service_role/secret). "
                         "Use a chave publica (anon/publishable)."}
    if not url.startswith("https://") or not key:
        return {"supabaseUrl": "", "supabaseKey": ""}
    return {"supabaseUrl": url, "supabaseKey": key}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(PAGE, "rb") as f:
                    self.send(200, f.read(), "text/html; charset=utf-8")
            elif u.path == "/api/ping":
                self.send_json(200, {"ok": True})
            elif u.path == "/api/rss":
                target = (q.get("url") or [""])[0]
                if urllib.parse.urlparse(target).hostname != "news.google.com":
                    return self.send_json(400, {"error": "host nao permitido"})
                code, body, ctype, _ = fetch(target)
                self.send(code, body, ctype)
            elif u.path == "/api/config":
                self.send_json(200, supabase_config())
            elif u.path == "/api/providers":
                self.send_json(200, {"providers": provider_list()})
            elif u.path == "/api/news":
                pid = (q.get("provider") or [""])[0]
                doms = ",".join(d for d in (x.strip().lower() for x in (q.get("domains") or [""])[0].split(","))
                                if re.fullmatch(r"[a-z0-9.-]{3,100}", d))
                ckeys = [k for k in self.headers.get("X-Api-Key", "").split(",") if k.strip()][:10]
                self.send_json(200, api_news(pid, (q.get("q") or [""])[0].strip(), ckeys, doms))
            elif u.path == "/api/feeds":
                topic = (q.get("topic") or [""])[0]
                raw = (q.get("outlets") or [""])[0]
                doms = [d for d in (x.strip().lower() for x in raw.split(","))
                        if re.fullmatch(r"[a-z0-9.-]{3,100}", d)][:12] or DEFAULT_DOMAINS
                self.send_json(200, collect(topic, doms))
            elif u.path == "/api/article":
                target = (q.get("url") or [""])[0]
                if not target.startswith("http"):
                    return self.send_json(400, {"error": "url invalida"})
                self.send_json(200, get_article(target))
            else:
                self.send(404, b"nao encontrado", "text/plain")
        except ApiError as e:
            self.send_json(e.code, {"error": str(e)})
        except ValueError as e:
            self.send_json(502, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.send_json(502, {"error": str(e), "status": "error", "msg": str(e)})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    # Na nuvem a plataforma define PORT e precisa que o servidor escute em 0.0.0.0.
    # No PC (sem PORT definida) continua so em 127.0.0.1.
    HOST = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"O Diario Virtual em http://localhost:{PORT}  (Ctrl+C para encerrar)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
