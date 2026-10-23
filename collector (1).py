#!/usr/bin/env python3
"""Ngai Group Archive collector.

Pulls recent papers from the tracked journals (Crossref), keeps the synthetic
organic chemistry ones, and merges them into data/papers.json.
Run every 2 days (see .github/workflows/update.yml).

  python collector.py                      # last 10 days
  python collector.py --since 2026-01-01   # backfill the year
"""
import argparse, difflib, html, json, os, re, sys, time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

MAILTO = os.environ.get("CROSSREF_EMAIL") or "archive@example.com"  # polite-pool contact
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "papers.json")

# (display name, ISSNs, minimum relevance score)
# Core organic journals need a low score; broad journals need stronger evidence.
JOURNALS = [
    ("J. Am. Chem. Soc.",      ["0002-7863", "1520-5126"], 2),
    ("Angew. Chem. Int. Ed.",  ["1433-7851", "1521-3773"], 2),
    ("ACS Catal.",             ["2155-5435"],              2),
    ("Nature Synthesis",       ["2731-0582"],              1),
    ("Nature Chemistry",       ["1755-4330", "1755-4349"], 2),
    ("Nature Catalysis",       ["2520-1158"],              3),
    ("Nature Communications",  ["2041-1723"],              3),
    ("Nature",                 ["0028-0836", "1476-4687"], 3),
    ("Science",                ["0036-8075", "1095-9203"], 3),
    ("Chem",                   ["2451-9294", "2451-9308"], 3),
]

# Relevance terms (strong = 2 points, weak = 1 point). Edit freely.
STRONG = ["total synthesis", "enantioselective", "diastereoselective", "asymmetric synthesis",
          "asymmetric catalysis", "cross-coupling", "c-h functionalization", "c-h activation",
          "c-h arylation", "c-h amination", "photoredox", "organocatal", "cycloaddition",
          "skeletal editing", "late-stage", "dearomati", "annulation", "hydrofunctionali",
          "hydroboration", "hydroamination", "carbene", "atroposelective", "kinetic resolution",
          "natural product synthesis", "radical cascade", "stereoselective synthesis"]
WEAK = ["synthesis", "synthesis of", "catalyzed", "catalysed", "catalytic", "stereoselective",
        "regioselective", "chemoselective", "alkene", "alkyne", "ketone", "amine", "amide",
        "heterocycle", "ligand", "nickel", "palladium", "copper", "photocatal", "electrochemical",
        "radical", "arylation", "alkylation", "amination", "borylation", "fluorination",
        "rearrangement", "cyclization", "olefin", "aryl", "carbonylation", "hydrogenation"]
# Anything matching these is dropped (inorganic, materials, polymers, biology...).
EXCLUDE = ["battery", "batteries", "perovskite", "supercapacitor", "zeolite", "metal-organic framework",
           r"\bmofs?\b", "covalent organic framework", r"\bcofs?\b", "nanoparticle", "nanosheet",
           "nanocluster", "nanocrystal", "quantum dot", "oxygen evolution", "hydrogen evolution",
           "co2 reduction", "co2 hydrogenation", "co2 electroreduction", "electrocatalyst",
           "single-atom", "photovoltaic", "solar cell", "polymerization", "polymer", "hydrogel",
           "protein", "cryo-em", "genom", "in vivo", "cell line", "antibod", "lithium", "sodium-ion",
           "water splitting", "heterogeneous catalyst", "thin film", "semiconductor", "alloy"]
SKIP_TITLE = ("cover", "front cover", "back cover", "outside", "inside", "correction", "author correction",
              "publisher correction", "erratum", "retraction", "editorial", "news", "research highlight",
              "introducing", "behind the science", "expression of concern", "masthead", "table of contents")

TAGS = [("total synthesis", "Total synthesis"), ("natural product", "Natural products"),
        ("enantio", "Asymmetric catalysis"), ("asymmetric", "Asymmetric catalysis"),
        ("skeletal editing", "Skeletal editing"), ("dearomati", "Dearomatization"),
        ("photoredox", "Photoredox"), ("photocatal", "Photocatalysis"), ("electrochem", "Electrochemistry"),
        ("c-h", "C–H functionalization"), ("cross-coupling", "Cross-coupling"),
        ("cycloaddition", "Cycloaddition"), ("carbene", "Carbene chemistry"),
        ("radical", "Radical chemistry"), ("fluor", "Fluorine chemistry"), ("organocatal", "Organocatalysis")]


def norm(s):
    s = re.sub(r"[\u2010-\u2015\u2212]", "-", s or "").lower()
    return re.sub(r"<[^>]+>", " ", s)


def score(text):
    t = norm(text)
    if any(re.search(p, t) for p in EXCLUDE):
        return -1
    return sum(2 for w in STRONG if w in t) + sum(1 for w in WEAK if w in t)


def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = Request(url, headers={"User-Agent": f"NgaiGroupArchive/1.0 (mailto:{MAILTO})"})
            with urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception as e:
            print("  retry", i + 1, e, file=sys.stderr)
            time.sleep(2 * (i + 1))
    return None


def parts_to_date(parts):
    if not parts:
        return None
    y, m, d = (parts + [1, 1])[:3] if len(parts) < 3 else parts[:3]
    try:
        return date(int(y), int(m or 1), int(d or 1))
    except ValueError:
        return None


def pub_date(it):
    for k in ("published-online", "issued", "published-print"):
        v = (it.get(k) or {}).get("date-parts", [[]])[0]
        d = parts_to_date(list(v)) if v else None
        if d:
            return min(d, date.today())
    return None


def clean_abstract(a):
    a = re.sub(r"<[^>]+>", " ", a or "")
    a = re.sub(r"\s+", " ", a).strip()
    return re.sub(r"^abstract[:.\s]*", "", a, flags=re.I)


def openalex_abstract(doi):
    j = get_json(f"https://api.openalex.org/works/https://doi.org/{quote(doi)}?mailto={MAILTO}&select=abstract_inverted_index")
    inv = (j or {}).get("abstract_inverted_index")
    if not inv:
        return ""
    words = {}
    for w, pos in inv.items():
        for p in pos:
            words[p] = w
    return " ".join(words[i] for i in sorted(words))


# ---------------------------------------------------------------- TOC / graphical-abstract images
# We store only the image URL (hot-linked, shown with a credit and a link to the paper), never a copy.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
ACS_PREFIX = {"jacs": "ja", "acscatal": "cs"}
BAD_IMG = ("logo", "default", "placeholder", "favicon", "sprite", "cover", "social", "banner")


def acs_image(doi):
    """ACS serves the TOC graphic at a predictable CDN path (loaded by the visitor's browser,
    which tries .gif/.jpeg/.jpg/.png in turn, so no server request to ACS is needed)."""
    m = re.match(r"10\.1021/(jacs|acscatal)\.([0-9a-z]+)$", doi.lower())
    if not m:
        return None
    return f"https://pubs.acs.org/cms/10.1021/{doi.split('/', 1)[1]}/asset/images/medium/{ACS_PREFIX[m[1]]}{m[2]}_0001.gif"


def page_image(url):
    """Read the og:image / twitter:image of an article page (works for Nature-family pages)."""
    try:
        req = Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html"})
        with urlopen(req, timeout=30) as r:
            page = r.read(400000).decode("utf-8", "ignore")
    except Exception as e:
        print("  image page failed:", url, e, file=sys.stderr)
        return None
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, page, re.I)
        if m:
            u = html.unescape(m.group(1))
            if u.startswith("http") and not any(b in u.lower() for b in BAD_IMG):
                return u
    return None


def find_image(doi):
    a = acs_image(doi)
    if a:
        return a
    if doi.startswith("10.1038/"):                       # Nature, Nat. Chem., Nat. Synth., Nat. Commun., Nat. Catal.
        u = page_image("https://www.nature.com/articles/" + doi.split("/", 1)[1])
        return u.replace("/lw1200/", "/lw685/") if u else None
    return page_image("https://doi.org/" + doi)          # Wiley, Science, Cell Press: works only if the site allows it


def fill_images(papers, retry=False):
    n = 0
    for p in papers:
        if not p.get("doi"):
            continue                                      # entries without a DOI: set "img" by hand
        if "img" in p and (p["img"] or not retry):
            continue
        p["img"] = find_image(p["doi"])
        n += 1
        time.sleep(0.6)
    return n


# ---------------------------------------------------------------- DOIs and full abstracts for existing entries
def crossref_abstract(doi):
    j = get_json(f"https://api.crossref.org/works/{quote(doi)}?mailto={MAILTO}")
    return clean_abstract(((j or {}).get("message") or {}).get("abstract"))


def fill_abstracts(papers, retry=False):
    """Replace short summaries with the publisher's abstract (Crossref first, then OpenAlex)."""
    n = 0
    for p in papers:
        if not p.get("doi") or ("ab_full" in p and (p["ab_full"] or not retry)):
            continue
        ab = crossref_abstract(p["doi"]) or openalex_abstract(p["doi"])
        if len(ab) > 80:
            p["ab"], p["ab_full"] = ab, True
        else:
            p["ab_full"] = False
        n += 1
        time.sleep(0.3)
    return n


def fill_doi(papers):
    """Find the DOI of entries that have none (matched by title, strict similarity)."""
    n = 0
    for p in papers:
        if p.get("doi"):
            continue
        q = urlencode({"query.bibliographic": p["t"], "rows": 3, "select": "DOI,title", "mailto": MAILTO})
        for it in ((get_json("https://api.crossref.org/works?" + q) or {}).get("message") or {}).get("items", []):
            t = (it.get("title") or [""])[0]
            if difflib.SequenceMatcher(None, norm(t), norm(p["t"])).ratio() > 0.93:
                p["doi"] = it["DOI"]
                n += 1
                break
        time.sleep(0.3)
    return n


def authors(it):
    a = [f"{x.get('given', '')} {x.get('family', '')}".strip() for x in it.get("author", []) if x.get("family") or x.get("name")]
    if not a:
        return ""
    return a[0] if len(a) == 1 else f"{a[0]} … {a[-1]}"


def fetch_journal(name, issns, since, min_score, known=frozenset()):
    seen, found = set(), []
    for issn in issns:
        cursor = "*"
        for _ in range(30):
            q = urlencode({"filter": f"issn:{issn},type:journal-article,from-created-date:{since}",
                           "select": "DOI,title,author,abstract,published-online,published-print,issued",
                           "rows": 100, "cursor": cursor, "mailto": MAILTO})
            j = get_json("https://api.crossref.org/works?" + q)
            msg = (j or {}).get("message", {})
            items = msg.get("items", [])
            if not items:
                break
            for it in items:
                doi = it.get("DOI", "").lower()
                title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", (it.get("title") or [""])[0])).strip()
                if not doi or doi in seen or doi in known or not title:
                    continue
                seen.add(doi)
                tl = title.lower()
                if tl.startswith(SKIP_TITLE):
                    continue
                d = pub_date(it)
                if not d:
                    continue
                ab = clean_abstract(it.get("abstract"))
                s = score(title + " " + ab)
                if s < 0:
                    continue
                if not ab:                       # title alone is thin evidence: look up the abstract
                    ab = openalex_abstract(doi)
                    s = score(title + " " + ab)
                    time.sleep(0.1)
                if s < min_score:
                    continue
                low = norm(title + " " + ab)
                tag = next((lab for k, lab in TAGS if k in low), "Synthetic methodology")
                found.append({"date": d.isoformat(), "j": name, "t": title, "au": authors(it),
                              "tag": tag, "ab": ab or "Abstract available at the publisher (DOI link).", "doi": it["DOI"], "ab_full": bool(ab), "img": find_image(it["DOI"])})
            cursor = msg.get("next-cursor")
            if not cursor:
                break
            time.sleep(0.3)
    return found


def write(by_key):
    year = date.today().year                                         # current year only
    out = sorted((p for p in by_key.values() if p["date"].startswith(str(year))), key=lambda p: p["date"], reverse=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD (default: 10 days ago)")
    ap.add_argument("--images-only", action="store_true", help="skip paper search, only fill missing DOIs, abstracts, images")
    ap.add_argument("--retry-images", action="store_true", help="also retry papers whose image was not found")
    ap.add_argument("--retry-abstracts", action="store_true", help="also retry papers whose full abstract was not found")
    a = ap.parse_args()
    since = a.since or (date.today() - timedelta(days=10)).isoformat()
    existing = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else []
    by_key = {(p.get("doi") or p["t"]).lower(): p for p in existing}   # existing entries (and manual edits) win
    known = {(p.get("doi") or "").lower() for p in existing}
    added = 0
    if not a.images_only:
        for name, issns, mn in JOURNALS:
            print(f"{name} …", flush=True)
            for p in fetch_journal(name, issns, since, mn, known):
                k = p["doi"].lower()
                if k not in by_key:
                    by_key[k] = p
                    added += 1
            write(by_key)                                            # checkpoint after every journal
    out = write(by_key)
    nd = fill_doi(out)
    na = fill_abstracts(out, retry=a.retry_abstracts)
    ni = fill_images(out, retry=a.retry_images)
    write(by_key)
    print(f"added {added}, DOIs found {nd}, abstracts looked up {na}, images looked up {ni}, total {len(out)} → {OUT}")


if __name__ == "__main__":
    main()
