#!/usr/bin/env python3
"""Hakee wwf.fi:n uutiset (Ajankohtaista) ja lausunto-PDF:t ja kirjoittaa ne
Markdown-tiedostoiksi Claude-projektin kontekstiksi.

Ympäristömuuttujat:
  YEAR       vuosi, jonka julkaisut haetaan (oletus: kuluva vuosi)
  OUT_DIR    tulostiedostojen kansio (oletus: context)
  CACHE_DIR  välimuistin kansio, jotta jo haettuja sivuja ei ladata uudelleen (oletus: cache)
"""
import hashlib, html, json, os, re, subprocess, sys, tempfile, time
import urllib.error, urllib.request
from datetime import date
from pathlib import Path
from bs4 import BeautifulSoup

BASE = "https://wwf.fi"
YEAR = int(os.environ.get("YEAR") or date.today().year)
OUT = Path(os.environ.get("OUT_DIR", "context"))
CACHE = Path(os.environ.get("CACHE_DIR", "cache"))
UA = "Mozilla/5.0 (compatible; WWF-Suomi-context-bot/1.0)"
KK = ["", "Tammikuu", "Helmikuu", "Maaliskuu", "Huhtikuu", "Toukokuu", "Kesäkuu",
      "Heinäkuu", "Elokuu", "Syyskuu", "Lokakuu", "Marraskuu", "Joulukuu"]
LAUSUNTO = re.compile(r"lausun|kannanot|vetoomus|budjettiriihi", re.I)
MUU = re.compile(r"vetoomus|kannanotto|budjettiriihi", re.I)


def get(url, binary=False, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 400:          # WordPress: sivu listan lopun jälkeen
                return None
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(5 * (i + 1))


def wp_list(endpoint, params, per_page=50):
    items, page = [], 1
    while True:
        q = "&".join(f"{k}={v}" for k, v in params.items())
        data = get(f"{BASE}/wp-json/wp/v2/{endpoint}?{q}&per_page={per_page}&page={page}")
        batch = json.loads(data) if data else []
        if not batch:
            return items
        items += batch
        page += 1


def clean(t):
    t = t.replace("\xad", "").replace("\u202f", " ").replace("\xa0", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"\s+([.,:;!?”])", r"\1", t)


def fi_date(iso):
    return f"{int(iso[8:10])}.{int(iso[5:7])}.{iso[:4]}"


# ---------- Uutiset ----------

def block_lines(el):
    out = []
    for e in el.find_all(["h2", "h3", "h4", "p", "li"]):
        if e.name == "p" and e.find_parent("li"):
            continue
        t = clean(e.get_text(" ", strip=True))
        if not t or "Juttu jatkuu" in t or t.startswith("Kuvan tekijänoikeudet"):
            continue
        out.append(f"#### {t}" if e.name in ("h2", "h3", "h4") else (f"- {t}" if e.name == "li" else t))
    if not out:
        t = clean(el.get_text(" ", strip=True))
        out = [t] if t else []
    return out


def parse_article(page_html):
    s = BeautifulSoup(page_html, "html.parser")
    art = s.find("article") or s
    au = art.find(class_="article__author-container")
    ing = art.find(class_="article__ingress")
    parts, contacts = [], []
    for b in art.find_all(class_=["text-block", "quote-block", "checkmark-list-block",
                                  "attachment-block", "person-card-block"]):
        c = b.get("class")
        if "text-block" in c:
            parts.append("\n\n".join(block_lines(b.find(class_="text-block__text") or b)))
        elif "quote-block" in c:
            d = b.find(class_="quote-block__desktop") or b
            q, m = d.find(class_="quote-block__quote"), d.find(class_="quote-block__meta")
            if q:
                parts.append(f"> {clean(q.get_text(' ', strip=True))}"
                             + (f" — {clean(m.get_text(' ', strip=True))}" if m else ""))
        elif "checkmark-list-block" in c:
            parts.append("**Tietolaatikko:**\n\n" + "\n".join(block_lines(b)))
        elif "attachment-block" in c:
            a = b.find("a", href=True)
            t = clean(b.get_text(" ", strip=True).replace("article-attachment-link", "")
                      .replace("Created with Sketch.", ""))
            parts.append(f"Liite: {t}" + (f" ({a['href']})" if a else ""))
        elif "person-card-block" in c:
            bits = [clean(x) for x in b.stripped_strings
                    if "@" not in x and "protected" not in x and not re.fullmatch(r"[\d\s+()-]{6,}", x)]
            if len(bits) >= 2:
                contacts.append(f"{bits[1]}, {bits[0]}")
    return {
        "author": clean(au.get_text(" ", strip=True)) if au else "",
        "ingress": clean(ing.get_text(" ", strip=True)) if ing else "",
        "parts": [p for p in parts if p.strip()],
        "contacts": list(dict.fromkeys(contacts)),
    }


def fetch_posts():
    cdir = CACHE / "posts"
    cdir.mkdir(parents=True, exist_ok=True)
    posts = wp_list("posts", {"after": f"{YEAR}-01-01T00:00:00",
                              "before": f"{YEAR + 1}-01-01T00:00:00", "_embed": "wp:term"})
    result = []
    for p in posts:
        cf = cdir / f"{p['id']}.json"
        cached = json.loads(cf.read_text()) if cf.exists() else None
        if cached and cached.get("modified") == p["modified"]:
            body = cached["body"]
        else:
            print("  haetaan:", p["link"], file=sys.stderr)
            body = parse_article(get(p["link"]))
            cf.write_text(json.dumps({"modified": p["modified"], "body": body}, ensure_ascii=False))
            time.sleep(1)
        cats = [t["name"] for g in p.get("_embedded", {}).get("wp:term", []) for t in g
                if t.get("taxonomy") == "category"]
        result.append({"date": p["date"], "title": clean(html.unescape(p["title"]["rendered"])),
                       "link": p["link"], "cats": cats, **body})
    return sorted(result, key=lambda x: (x["date"], x["link"]), reverse=True)


def write_news(posts):
    head = [f"# WWF Suomi – Ajankohtaista: vuoden {YEAR} julkaisut", "",
            "Lähde: https://wwf.fi/ajankohtaista/ – kaikki wwf.fi:n uutisartikkelit "
            f"1.1.{YEAR} alkaen. Päivittyy automaattisesti kerran päivässä.",
            f"Artikkeleita: {len(posts)}"
            + (f" (uusin {fi_date(posts[0]['date'])})" if posts else ""),
            "Kuvat ja kuvatekstit jätetty pois; yhteyshenkilöistä mukana nimi ja titteli.", "",
            "## Sisällysluettelo"]
    toc, body, cur = [], [], None
    for p in posts:
        m = int(p["date"][5:7])
        if m != cur:
            cur = m
            toc.append(f"\n**{KK[m]} {YEAR}**\n")
            body.append(f"\n## {KK[m]} {YEAR}\n")
        toc.append(f"- {fi_date(p['date'])} – {p['title']}")
        meta = [f"Julkaistu: {fi_date(p['date'])}", f"Aiheet: {', '.join(p['cats']) or '–'}"]
        if p["author"]:
            meta.append(f"Kirjoittaja: {p['author']}")
        meta.append(f"Linkki: {p['link']}")
        parts = [f"### {p['title']}\n", "  \n".join(meta) + "\n"]
        if p["ingress"]:
            parts.append(f"**{p['ingress']}**\n")
        parts += p["parts"]
        if p["contacts"]:
            parts.append("Lisätietoja: " + "; ".join(p["contacts"]))
        body.append("\n\n".join(parts) + "\n\n---\n")
    return "\n".join(head + toc) + "\n\n---\n" + "\n".join(body)


# ---------- Lausunnot ----------

def pdf_text(data):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        r = subprocess.run(["pdftotext", "-enc", "UTF-8", f.name, "-"], capture_output=True)
    t = r.stdout.decode("utf-8", "replace").replace("\f", "\n\n").replace("\xad", "")
    paras = []
    for blk in re.split(r"\n\s*\n", t):
        lines = [l.strip() for l in blk.split("\n") if l.strip()]
        if not lines or (len(lines) == 1 and re.fullmatch(r"\d{1,3}(\s*\(\d+\))?|\d+\s*/\s*\d+", lines[0])):
            continue
        s = ""
        for l in lines:
            if s.endswith("-") and l[:1].islower():
                s = s[:-1] + l
            elif re.match(r"^([•▪\-–·]|\d+[.)])\s", l):
                s += ("\n" if s else "") + l
            else:
                s += (" " if s else "") + l
        paras.append(s)
    return "\n\n".join(paras)


def doc_title(raw, text):
    t = html.unescape(raw).replace("_", " ")
    if "Lausuntopyynt" in t and text:
        m = re.search(r"(Lausuntopyyntö.*?)(?:Väliotsikot|Lausunnonantajan|$)", re.sub(r"\s+", " ", text[:800]))
        if m:
            return "WWF:n lausunto – " + m[1].strip()
    t = re.sub(r"^\d{8}\s*", "", t)
    t = re.sub(r"^WWF lausunto WWFn", "WWF:n", t)
    t = re.sub(r"\bWWFn\b", "WWF:n", t)
    t = re.sub(r"\bEUn\b", "EU:n", t)
    return re.sub(r"\s+", " ", t).strip()


def doc_date(d):
    m = re.search(r"Lausunto\s+(\d{2})\.(\d{2})\.(\d{4})", d["text"][:400])
    if m:
        return f"{int(m[1])}.{int(m[2])}.{m[3]}", True
    return fi_date(d["date"]), False


def fetch_statements():
    cdir = CACHE / "pdf"
    cdir.mkdir(parents=True, exist_ok=True)
    media = wp_list("media", {"mime_type": "application/pdf",
                              "after": f"{YEAR}-01-01T00:00:00", "before": f"{YEAR + 1}-01-01T00:00:00"})
    by_url = {}
    for m in media:
        url = m["source_url"].split("?")[0]
        if LAUSUNTO.search(m["title"]["rendered"] + url):
            by_url.setdefault(url, m)
    docs, hashes = [], set()
    for url, m in by_url.items():
        cf = cdir / (hashlib.sha1(url.encode()).hexdigest() + ".json")
        if cf.exists():
            c = json.loads(cf.read_text())
        else:
            print("  haetaan:", url, file=sys.stderr)
            data = get(m["source_url"], binary=True)
            c = {"hash": hashlib.md5(data).hexdigest(), "text": pdf_text(data)}
            cf.write_text(json.dumps(c, ensure_ascii=False))
            time.sleep(1)
        if c["hash"] in hashes:          # sama PDF ladattu kahdesti
            continue
        hashes.add(c["hash"])
        d = {"date": m["date"], "url": url, "text": c["text"]}
        d["title"] = doc_title(m["title"]["rendered"], c["text"])
        d["shown_date"], d["own_date"] = doc_date(d)
        d["sort"] = tuple(int(x) for x in reversed(d["shown_date"].split(".")))
        d["other"] = bool(MUU.search(m["title"]["rendered"]))
        docs.append(d)
    return sorted(docs, key=lambda d: (d["sort"], d["date"], d["url"]))


def write_statements(docs):
    ls = [d for d in docs if not d["other"]]
    ot = [d for d in docs if d["other"]]
    L = [f"# WWF Suomi – lausunnot ja kannanotot {YEAR}", "",
         f"Lähde: wwf.fi:n mediakirjastoon vuonna {YEAR} ladatut lausunto- ja kannanotto-PDF:t. "
         "Päivittyy automaattisesti kerran päivässä.",
         f"Lausuntoja: {len(ls)} · muita kannanottoja ja vetoomuksia: {len(ot)}. "
         "Tekstit poimittu PDF:istä; taulukoiden ja palstojen muotoilu voi olla hajonnut.",
         "Päivämäärä on lausunnon oma päiväys, jos se löytyi asiakirjasta; muuten wwf.fi:hin latauspäivä "
         "(merkitty ”ladattu”).",
         "Rajaus: mukana vain wwf.fi:ssä julkaistut asiakirjat.", "", "## Sisällysluettelo", ""]
    for label, lst in (("Lausunnot", ls), ("Muut kannanotot ja vetoomukset", ot)):
        L += [f"**{label}**", ""]
        L += [f"- {d['shown_date']}{'' if d['own_date'] else ' (ladattu)'} – {d['title']}" for d in lst]
        L.append("")
    L += ["---", ""]
    for label, lst in (("Lausunnot", ls), ("Muut kannanotot ja vetoomukset", ot)):
        L.append(f"## {label}\n")
        for d in lst:
            meta = [f"Päiväys: {d['shown_date']}" if d["own_date"] else f"Ladattu wwf.fi:hin: {d['shown_date']}"]
            a = re.search(r"Asia:\s*(VN/[\d/]+)", d["text"][:600])
            if a:
                meta.append(f"Diaarinumero: {a[1]}")
            meta.append(f"PDF: {d['url']}")
            L += [f"### {d['title']}\n", "  \n".join(meta) + "\n", d["text"], "\n---\n"]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(L))


def save(path, text):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old != text:
        path.write_text(text, encoding="utf-8")
        print(f"Päivitetty: {path}")
    else:
        print(f"Ei muutoksia: {path}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Haetaan uutiset vuodelta {YEAR}...")
    posts = fetch_posts()
    save(OUT / f"wwf-ajankohtaista-{YEAR}.md", write_news(posts))
    print(f"Haetaan lausunnot vuodelta {YEAR}...")
    docs = fetch_statements()
    save(OUT / f"wwf-lausunnot-{YEAR}.md", write_statements(docs))
    print(f"Valmis: {len(posts)} uutista, {len(docs)} lausuntoa/kannanottoa.")


if __name__ == "__main__":
    main()
