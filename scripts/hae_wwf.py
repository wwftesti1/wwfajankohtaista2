#!/usr/bin/env python3
"""Hakee wwf.fi:n Ajankohtaista-arkiston kaikki sisältötyypit (uutiset, mediatiedotteet,
näkökulmat, lausunnot, raportit, WWF-lehti, oppaat, asiantuntijakirjoitukset ...) ja kirjoittaa
ne Markdown-tiedostoiksi Claude-projektin kontekstiksi.

Lähde on sama rajapinta, jota wwf.fi/ajankohtaista-sivu käyttää (/wp-json/wwffi/v1/archive).

Ympäristömuuttujat:
  START_YEAR  ensimmäinen haettava vuosi (oletus: 2021); haetaan siitä kuluvaan päivään
  OUT_DIR     tulostiedostojen kansio (oletus: context)
  CACHE_DIR   välimuisti, jotta jo haettuja sivuja ja PDF:iä ei ladata uudelleen (oletus: cache)
  REFRESH_DAYS näin tuoreet kohteet haetaan aina uudelleen muokkausten varalta (oletus: 30)
  WORKERS     samanaikaisten latausten määrä (oletus: 3)
"""
import hashlib, html, json, os, re, shutil, subprocess, sys, tempfile, time
import urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from bs4 import BeautifulSoup

BASE = "https://wwf.fi"
START_YEAR = int(os.environ.get("START_YEAR") or 2021)
OUT = Path(os.environ.get("OUT_DIR", "context"))
CACHE = Path(os.environ.get("CACHE_DIR", "cache"))
REFRESH_DAYS = int(os.environ.get("REFRESH_DAYS") or 30)
WORKERS = int(os.environ.get("WORKERS") or 3)
UA = "Mozilla/5.0 (compatible; WWF-Suomi-context-bot/1.0)"
KK = ["", "Tammikuu", "Helmikuu", "Maaliskuu", "Huhtikuu", "Toukokuu", "Kesäkuu",
      "Heinäkuu", "Elokuu", "Syyskuu", "Lokakuu", "Marraskuu", "Joulukuu"]

# Ryhmät: nimi -> (tiedoston tunniste, tiedosto per vuosi?)
GROUPS = {
    "Uutiset": ("uutiset", True),
    "Mediatiedotteet": ("mediatiedotteet", True),
    "Näkökulmat": ("nakokulmat", False),
    "Asiantuntijakirjoitus": ("asiantuntijakirjoitukset", False),
    "Lausunnot": ("lausunnot", True),
    "Raportit": ("raportit", False),
    "Opas": ("oppaat", False),
    "WWF-lehti": ("wwf-lehti", True),
    "Suojelusaavutus": ("suojelusaavutukset", False),
    "Lahjoittajatarina": ("lahjoittajatarinat", False),
    "Lahjoita": ("lahjoita", False),
    "Eläinlajit": ("elainlajit", False),
    "Uhat": ("uhat", False),
    "Sivu": ("sivut", False),
    "Muut aineistot": ("muut-aineistot", False),
}
RAPORTTI = re.compile(r"raport|selvitys|tutkimu|kyselytutkimus|tulokset|report", re.I)
LAUSUNTO = re.compile(r"lausun|huomiot|kannanot|vetoomus|budjettiriihi|asiantuntijalausun", re.I)


def get(url, binary=False, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 410):
                return None
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(5 * (i + 1))


def clean(t):
    t = (t or "").replace("\xad", "").replace("\u202f", " ").replace("\xa0", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"\s+([.,:;!?”])", r"\1", t)


def parse_date(s):
    d, m, y = (int(x) for x in s.split("."))
    return date(y, m, d)


def fi(d):
    return f"{d.day}.{d.month}.{d.year}"


# ---------- Hakemisto ----------

def fetch_index():
    """Käy arkiston sivuja (uusin ensin), kunnes ollaan START_YEARia vanhemmissa."""
    items, page = [], 1
    while True:
        data = get(f"{BASE}/wp-json/wwffi/v1/archive?page={page}")
        posts = json.loads(data).get("posts", []) if data else []
        if not posts:
            break
        old = 0
        for p in posts:
            if not p.get("date"):
                continue
            d = parse_date(p["date"])
            if d.year >= START_YEAR:
                items.append({**p, "_date": d})
            else:
                old += 1
        if old == len(posts):
            break
        page += 1
    return items


def group_of(p):
    ptype = p["id"].split("#")[0]
    ct = p.get("content_type")
    if ct:
        return ct, False
    if ptype == "wwffi_press_release":
        return "Mediatiedotteet", True
    if ptype == "wwffi_mag_article":
        return "WWF-lehti", True
    if ptype == "wwffi_material_bank":
        title = p.get("title") or ""
        if LAUSUNTO.search(title):
            return "Lausunnot", True
        if RAPORTTI.search(title):
            return "Raportit", True
        return "Muut aineistot", False
    return "Uutiset", True


# ---------- Sisällön poiminta ----------

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
    art = s.find("article") or s.find("main") or s
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
    if not parts:  # tavallinen sivu ilman artikkelilohkoja
        main = s.find("main") or s
        for t in main(["script", "style", "nav", "header", "footer", "form", "noscript"]):
            t.decompose()
        parts = ["\n\n".join(block_lines(main))]
    return {"author": clean(au.get_text(" ", strip=True)) if au else "",
            "ingress": clean(ing.get_text(" ", strip=True)) if ing else "",
            "text": "\n\n".join(p for p in parts if p.strip()),
            "contacts": list(dict.fromkeys(contacts))}


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


def kind_of(p):
    url = p["url"].split("?")[0].lower()
    if url.endswith(".pdf") and url.startswith(BASE):
        return "pdf"
    if not url.startswith(BASE) or re.search(r"\.(jpe?g|png|gif|xlsx?|docx?|pptx?|zip)$", url):
        return "link"          # ulkoinen linkki tai muu tiedosto: vain otsikko ja linkki
    return "html"


def fetch_content(p, used):
    kind = kind_of(p)
    if kind == "link":
        return {"kind": "link"}
    key = hashlib.sha1(p["url"].split("?")[0].encode()).hexdigest()
    cf = CACHE / "items" / f"{key}.json"
    used.add(cf.name)
    fresh = p["_date"] >= date.today() - timedelta(days=REFRESH_DAYS)
    if cf.exists() and not (fresh and kind == "html"):
        return json.loads(cf.read_text())
    print("  haetaan:", p["url"].split("?")[0], file=sys.stderr)
    try:
        if kind == "pdf":
            data = get(p["url"], binary=True)
            c = {"kind": "pdf", "hash": hashlib.md5(data).hexdigest(), "text": pdf_text(data)} if data else None
        else:
            page = get(p["url"])
            c = {"kind": "html", **parse_article(page)} if page else None
    except Exception as e:
        print(f"  VIRHE {p['url']}: {e}", file=sys.stderr)
        return {"kind": "error"}
    if c is None:
        return {"kind": "error"}
    cf.write_text(json.dumps(c, ensure_ascii=False))
    return c


# ---------- Markdown ----------

def statement_date(text):
    m = re.search(r"Lausunto\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", text[:400])
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            return None
    return None


def render(group, items, year):
    slug, _ = GROUPS.get(group, (group.lower(), False))
    period = str(year) if year else f"{START_YEAR}–"
    inferred = sum(1 for i in items if i["inferred"])
    L = [f"# WWF Suomi – {group} {period}", "",
         f"Lähde: https://wwf.fi/ajankohtaista/ (sisältötyyppi: {group}). Päivittyy automaattisesti kerran päivässä.",
         f"Kohteita: {len(items)}" + (f" (uusin {fi(items[0]['_date'])})" if items else "") + ".",
         "Kuvat jätetty pois; PDF-tekstien taulukko- ja palstamuotoilu voi olla hajonnut. "
         "Ulkoisista linkeistä (esim. lehtien mielipidekirjoitukset) mukana vain otsikko ja linkki."]
    if inferred:
        L.append(f"Huom: {inferred} kohteelta puuttuu sivustolla sisältötyyppi; ne on luokiteltu kohdetyypin "
                 "tai otsikon perusteella (merkitty ”luokiteltu”).")
    L += ["", "## Sisällysluettelo"]
    toc, body, cur = [], [], None
    for i in items:
        d = i["_date"]
        if year and d.month != cur:
            cur = d.month
            toc.append(f"\n**{KK[d.month]} {d.year}**\n")
            body.append(f"\n## {KK[d.month]} {d.year}\n")
        elif not year and d.year != cur:
            cur = d.year
            toc.append(f"\n**{d.year}**\n")
            body.append(f"\n## {d.year}\n")
        title = clean(html.unescape(i["title"] or ""))
        toc.append(f"- {fi(d)} – {title}")
        c = i["content"]
        meta = [f"Julkaistu: {fi(d)}"]
        sd = statement_date(c.get("text", "")) if c.get("kind") == "pdf" else None
        if sd and sd != d:
            meta.append(f"Asiakirjan päiväys: {fi(sd)}")
        cats = ", ".join(x.get("name", "") for x in (i.get("categories") or []) if isinstance(x, dict))
        if cats:
            meta.append(f"Aiheet: {cats}")
        if c.get("author"):
            meta.append(f"Kirjoittaja: {c['author']}")
        if i["inferred"]:
            meta.append("Sisältötyyppi: luokiteltu")
        meta.append(f"Linkki: {i['url'].split('?')[0]}")
        parts = [f"### {title}\n", "  \n".join(meta) + "\n"]
        if c.get("ingress"):
            parts.append(f"**{c['ingress']}**\n")
        if c.get("text"):
            parts.append(c["text"])
        elif c.get("kind") == "link":
            parts.append("_(Ulkoinen linkki tai muu kuin tekstitiedosto – sisältöä ei poimittu.)_")
        elif c.get("kind") == "error":
            parts.append("_(Sisällön haku epäonnistui tällä kertaa.)_")
        if c.get("contacts"):
            parts.append("Lisätietoja: " + "; ".join(c["contacts"]))
        body.append("\n\n".join(parts) + "\n\n---\n")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(L + toc) + "\n\n---\n" + "\n".join(body))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (CACHE / "items").mkdir(parents=True, exist_ok=True)
    print(f"Haetaan arkiston hakemisto ({START_YEAR}–)...")
    index = fetch_index()
    print(f"  {len(index)} kohdetta")
    used = set()
    with ThreadPoolExecutor(WORKERS) as ex:
        contents = list(ex.map(lambda p: fetch_content(p, used), index))
    groups, seen_pdf = {}, set()
    for p, c in zip(index, contents):
        if c.get("kind") == "pdf":           # sama PDF voi olla listattu kahdesti
            if c["hash"] in seen_pdf:
                continue
            seen_pdf.add(c["hash"])
        g, inferred = group_of(p)
        groups.setdefault(g, []).append({**p, "content": c, "inferred": inferred})
    written = set()
    for g, items in groups.items():
        items.sort(key=lambda i: (i["_date"], i["url"]), reverse=True)
        slug, per_year = GROUPS.get(g, (re.sub(r"[^a-z0-9]+", "-", g.lower()), False))
        if per_year:
            for y in sorted({i["_date"].year for i in items}):
                fn = f"wwf-{slug}-{y}.md"
                save(OUT / fn, render(g, [i for i in items if i["_date"].year == y], y))
                written.add(fn)
        else:
            fn = f"wwf-{slug}.md"
            save(OUT / fn, render(g, items, None))
            written.add(fn)
        print(f"{g}: {len(items)}")
    for f in OUT.glob("*.md"):               # poista vanhentuneet tiedostot
        if f.name not in written:
            f.unlink()
            print(f"Poistettu: {f}")
    for f in (CACHE / "items").glob("*.json"):  # siivoa välimuisti
        if f.name not in used:
            f.unlink()
    for old in ("posts", "pdf"):             # aiemman skriptiversion välimuisti
        shutil.rmtree(CACHE / old, ignore_errors=True)


def save(path, text):
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old != text:
        path.write_text(text, encoding="utf-8")
        print(f"Päivitetty: {path}")


if __name__ == "__main__":
    main()
