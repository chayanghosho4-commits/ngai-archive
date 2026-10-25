#!/usr/bin/env python3
"""Ngai Group Archive collector.

Pulls recent papers from the tracked journals (Crossref), keeps only organic-synthesis
papers (new reactions and catalytic methods, total synthesis, skeletal editing, etc.),
and merges them into data/papers.json.
Run every 2 days (see .github/workflows/update.yml).

  python collector.py                      # last 10 days
  python collector.py --since 2026-01-01   # backfill the year

Version 3 (Oct 2026):
- Much stricter topic filter. A paper is kept only if its TITLE names a synthetic reaction,
  method or strategy (see SIGNALS), and nothing in it points to materials, energy,
  biology, spectroscopy, reviews, etc. (see EXCLUDE_ANY / EXCLUDE_TITLE).
  Papers already in data/papers.json that fail the filter are removed on the next run
  (listed in the log). Add  "keep": true  to an entry to protect it, and list DOIs in
  data/blocked.txt (one per line) to remove a paper for good.
- Nature-family images are built from the DOI (Figure 1 on media.springernature.com),
  so no request to nature.com is needed.
- Angew. Chem. images/abstracts come from the Wiley RSS feed; reviews, covers and other
  non-research items in that feed are skipped.
- Only temporary errors are retried; failures name their URL; a publisher that refuses
  3 image requests in a row is skipped for the rest of the run; time budget (MAX_MINUTES,
  default 300) saves before the workflow limit.
"""
import argparse, difflib, html, json, os, re, sys, time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

MAILTO = os.environ.get("CROSSREF_EMAIL") or "archive@example.com"  # polite-pool contact
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "papers.json")
BLOCKED = os.path.join(HERE, "data", "blocked.txt")
UA = f"NgaiGroupArchive/1.0 (mailto:{MAILTO})"
MAX_PAGES = 200                                                   # x 100 records per ISSN
DEADLINE = time.time() + float(os.environ.get("MAX_MINUTES") or 300) * 60

# (display name, ISSNs, minimum score)
# Score = 2 per synthesis signal in the title + 1 per signal in the abstract (max 3).
# 2 = one clear signal in the title is enough (synthesis-heavy journals).
# 3 = needs a second signal (broad journals with many non-synthesis papers).
JOURNALS = [
    ("J. Am. Chem. Soc.",      ["0002-7863", "1520-5126"], 2),
    ("Angew. Chem. Int. Ed.",  ["1433-7851", "1521-3773"], 2),
    ("ACS Catal.",             ["2155-5435"],              2),
    ("Nature Synthesis",       ["2731-0582"],              2),
    ("Nature Chemistry",       ["1755-4330", "1755-4349"], 2),
    ("Nature Catalysis",       ["2520-1158"],              3),
    ("Nature Communications",  ["2041-1723"],              3),
    ("Nature",                 ["0028-0836", "1476-4687"], 3),
    ("Science",                ["0036-8075", "1095-9203"], 3),
    ("Chem",                   ["2451-9294", "2451-9308"], 3),
]
MIN_SCORE = {name: mn for name, _, mn in JOURNALS}

# ---------------------------------------------------------------- topic filter (edit freely)
# Text is lower-cased and all dashes become "-" before matching (C–H -> c-h).
# SIGNALS: reaction types, strategies and catalysis words typical of organic synthesis papers.
SIGNALS = [
    # target-oriented synthesis
    r"total synthes", r"formal synthes", r"\bsynthesis of\b", r"natural products?\b",
    r"(asymmetric|enantioselective|divergent|concise|collective|modular|scalable|unified|"
    r"bioinspired|biomimetic|chemoenzymatic|de novo|programmable|stereoselective) synthes",
    # stereochemistry
    r"enantio(selective|divergent|convergent|specific|enriched)", r"diastereo(selective|divergent)",
    r"stereo(selective|specific|divergent|controlled|retentive|invertive)", r"atroposelective",
    r"\basymmetric\b", r"desymmetri", r"kinetic resolution", r"deracemi",
    # bond formation / named reaction classes
    r"cross-coupling", r"(reductive|oxidative|decarboxylative|deaminative|deoxygenative|dehydrogenative|"
    r"radical|suzuki|negishi|stille|kumada|hiyama|sonogashira|heck|buchwald|chan-lam|ullmann|"
    r"heteroatom|amide|c-[a-z]+|\)-[a-z]+) coupling", r"coupling of (aryl|alkyl|alkenyl|amines|alcohols|"
    r"carboxylic|boronic|olefins|alkenes|alkynes|ketones|aldehydes)",
    r"\bc-h\b", r"c\(sp[23]?\)", r"\bc-(c|n|o|s|f|b|si|p|x) (bond|activation|cleavage|formation)",
    r"cyclopropanat", r"aziridinat", r"epoxidat", r"cycloaddition", r"diels-alder", r"annulation",
    r"cycli[sz]ation", r"macrocycli[sz]", r"dearomati", r"skeletal (editing|rearrangement|transmutation|remodel)",
    r"ring (expansion|contraction|opening|closing)", r"(atom|nitrogen|carbon|oxygen|sulfur) (insertion|deletion|transmutation|swap)",
    r"single-atom (editing|insertion|deletion|skeletal)",
    r"hydro(functionali|amination|arylation|alkylation|alkenylation|alkynylation|boration|silylation|"
    r"acylation|carboxylation|formylation|cyanation|halogenation|thiolation|sulfonylation|oxygenation|"
    r"aminoalkylation|phosphination|carbonylation|esterification)",
    r"difunctionali", r"carbo(amination|functionali|boration|metalation|xylation)",
    r"amino(arylation|alkylation|hydroxylation|oxygenation)", r"oxy(amination|arylation)",
    r"borylat", r"silylat", r"stannylat", r"fluorinat", r"fluoroalkylat", r"trifluoromethyl", r"difluoromethyl",
    r"pentafluorosulfanyl", r"aminat(ion|ive|ing)", r"amidation", r"\barylat", r"\balkylat", r"alkenylat", r"alkynylat",
    r"\bacylat", r"carbonylation", r"(?<!de)carboxylation", r"cyanation", r"olefinat", r"metathesis", r"halogenation",
    r"chlorination", r"bromination", r"iodination", r"esterification", r"etherification", r"sulfonylation", r"thiolation",
    r"glycosylat",
    r"deoxygenat", r"decarboxylat", r"deaminat", r"dehydrogenat", r"desulfur", r"dehalogenat", r"deuterat",
    r"isotop(e|ic) (labell?ing|exchange)", r"homologat", r"rearrangement", r"transposition",
    r"(1,[2-5])-(migration|shift|transposition|addition)", r"(aryl|acyl|boron|group|radical|functional group) migration",
    r"isomeri[sz]ation", r"translocation", r"late-stage", r"umpolung", r"functionali[sz]ation",
    r"(asymmetric|transfer|selective|enantioselective) hydrogenation", r"reductive (amination|alkylation|arylation|functionali)",
    r"oxidative (amination|functionali|cyclization|cleavage of)", r"\b(alcohol|alkene|amine|aldehyde|ketone|olefin|arene|"
    r"benzylic|allylic|c-h) (oxidation|reduction|oxygenation)", r"(oxidation|reduction) of (alcohols|alkenes|olefins|"
    r"ketones|aldehydes|amides|esters|carboxylic|nitriles|arenes|amines|sulfides)",
    r"wolff-kishner|mitsunobu|wittig|aldol|mannich|michael addition|friedel-crafts|claisen|cope|nazarov|pauson-khand|"
    r"birch|minisci|ugi|passerini|baeyer-villiger|beckmann|curtius|appel|corey|evans|sharpless|tsuji-trost|morita-baylis",
    # reactive intermediates and mechanisms used in synthesis
    r"radical (cascade|relay|cyclization|addition|coupling|clock|cross|sorting|polar|translocation|"
    r"functionali|deoxygenation)", r"\b(aryl|alkyl|acyl|amidyl|ketyl|vinyl|allyl|benzylic|alkoxy|iminyl|"
    r"sulfonyl|carbamoyl|silyl|boryl|phosphoranyl) radicals?\b", r"hydrogen[- ]atom transfer",
    r"halogen[- ]atom transfer", r"\b(m?hat|xat)\b", r"carbene", r"nitrene", r"\bdiazo", r"ylides?\b",
    r"carbocation", r"carbanion", r"aryne", r"\bbenzyne",
    # catalysis and activation modes
    r"photoredox", r"metallaphotoredox", r"energy[- ]transfer catal", r"photocatalytic (synthesis|coupling|"
    r"functionali|amination|alkylation|arylation|deoxygenation|cyclization|decarboxyl|c-h|hydro)",
    r"photochemical (synthesis|reaction|rearrangement|cycloaddition|strategy|approach)",
    r"(visible-)?light-(mediated|driven|induced|promoted|enabled)", r"electrosynthes", r"electrochemical (synthesis|"
    r"cross-coupling|coupling|functionali|amination|carboxylation|oxidation of|reduction of|c-h)",
    r"electro(reductive|oxidative)", r"organocatal", r"n-heterocyclic carbene", r"lewis (acid|base) catal",
    r"(bronsted|brønsted) acid catal", r"phosphoric acid", r"chiral (ligand|catalyst|amine|phosphine|lewis)",
    r"[\w)\]]-catal[ysz]", r"\b(pd|ni|cu|co|fe|rh|ir|ru|au|ag|mn|ti|zn|sm|in|bi|b|si|sn|light|photo|electro|base|"
    r"acid|iodine|radical|nhc|halogen|silyl|boron|metal|enzyme)(\([iv0]+\))?-mediated", r"(palladium|nickel|copper|cobalt|iron|rhodium|iridium|"
    r"ruthenium|gold|silver|manganese|titanium|chromium|platinum|zinc|scandium|boron|bismuth|samarium|"
    r"lanthanum|indium|vanadium|tungsten|molybdenum|zirconium|hafnium|aluminum|aluminium) (catal|hydride)",
    r"metal hydride", r"biocatal", r"chemoenzymatic", r"enzymatic (synthesis|cascade|c-h|amination|cyclization|"
    r"halogenation|hydroxylation)", r"oligosaccharide", r"carbohydrate synthesis",
    # motifs medicinal / methods chemists target
    r"\bsynthes(is|es|ize[sd]?|izing)\b", r"\baccess(ing)? to\b", r"\bto access\b", r"regio(selective|divergent|specific)",
    r"chemoselective", r"site-selective", r"(allyl|benzyl|propargyl|vinyl|methyl|prenyl|crotyl|cyclopropyl)ation",
    r"\bdeuter", r"skeleta?l?\b|skeleton", r"isotope incorporation", r"atom-pair exchange", r"atom (exchange|swap|migration|transfer)",
    r"scaffold hopping", r"medicinal chemistry", r"cyclo\w*(yne|triene|allene)s?\b", r"\bradicals?\b", r"disconnection",
    r"direct conversion of", r"[a-z0-9)\]]-mediated", r"multicomponent", r"cascade", r"photocycloaddition", r"photoenzym",
    r"photocataly", r"carbon-(carbon|nitrogen|oxygen|heteroatom|halogen|fluorine|boron) bonds?",
    r"(alkene|alkyne|olefin|nucleophile|electrophile|cross-electrophile|cross-nucleophile|aryl|alkyl|c-c|c-n|c-o) coupling",
    r"\bediting\b", r"\blmct\b", r"conversion of", r"atropisomer", r"(axially|planar|inherently|helically) chiral",
    r"stereogenic", r"thianthren", r"\b(sn2|snar|sn1)\b", r"homolytic substitution", r"nucleophilic substitution",
    r"bioisoster", r"bicyclo\[", r"\bbcps?\b", r"cubane", r"sulfoximine", r"sulfinamide", r"sulfonimidamide",
    r"sulfonyl fluoride", r"sulfur\(vi\)", r"\bsufex\b", r"\bclick (chemistry|reaction)",
]
# Organic structure words: half weight (1 point instead of 2), so a title needs two of them,
# or one of them plus a reaction word from SIGNALS.
MOTIFS = [
    r"aziridin", r"azetidin", r"oxetan", r"cyclopropan", r"cyclobut", r"\bspiro", r"ketenimin", r"ynamid", r"\ballenes?\b",
    r"enamin", r"enamid", r"\benol", r"\benones?\b", r"ynones?\b", r"lactams?\b", r"lactones?\b", r"indol", r"pyrrol",
    r"pyridin", r"quinolin", r"piperidin", r"morpholin", r"azepan", r"sulfonamid", r"sulfones?\b", r"thioethers?\b",
    r"glycosid", r"nucleosid", r"peptides?\b", r"amino acids?\b", r"carbocycl", r"heterocycl", r"heteroaren",
    r"\b(alkenes?|alkynes?|olefins?|styrenes?|dienes?|arenes?|alkanes?|ketones?|aldehydes?|carbonyls?|esters?|amides?|"
    r"amines?|alcohols?|ethers?|carboxylic acids?|aryl halides?|alkyl halides?|boronic|boronates?|silanes?|epoxides?|"
    r"imines?|nitriles?|thiols?|phenols?|anilines?|azides?|hydrazones?|oximes?|organoboron|grignard)\b",
]

# HARD exclusions: another field. Drop if found (title+abstract for EXCLUDE_ANY, title for EXCLUDE_TITLE).
# (Words like anode, cathode, electrolyte, TADF or hydrogen evolution are normal in synthesis
#  abstracts, so they only count in the title.)
EXCLUDE_ANY = [
    r"batter(y|ies)", r"supercapacitor", r"perovskite", r"photovoltaic", r"solar cell", r"\boleds?\b",
    r"electroluminesc", r"metal-organic framework", r"\bmofs?\b", r"covalent organic framework", r"\bcofs?\b",
    r"hydrogen-bonded organic framework", r"zeolite", r"quantum dot", r"nanozyme",
    r"single[- ]atoms?[- ]([a-z]{1,2}[- ])?(catalyst|site|metal|alloy)", r"single-atom-catal", r"\bco2rr\b",
    r"water splitting", r"nitrogen fixation", r"fuel cell", r"triboelectric", r"piezoelectric", r"ferroelectric",
    r"thermoelectric", r"superconduct", r"spintronic", r"qubit",
    r"(lithium|sodium|zinc|potassium|magnesium)[- ]ion (batter|storage|cell|conduct)",
    r"\b(li|na|zn|k)-ion\b", r"\b(li|na|zn)-metal\b", r"hydrogel", r"aerogel", r"liquid crystal", r"mxene",
    r"cryo-em", r"\bcrispr", r"\bgenom", r"\bneurons?\b", r"microbiome", r"vaccine", r"drug delivery", r"photothermal",
    r"photodynamic therapy", r"theranostic", r"terahertz", r"phonon", r"\bx-ray (scattering|emission|free-electron)",
    r"faradaic efficienc", r"ampere-level", r"industrial(-level| level) current", r"fischer-tropsch",
]
EXCLUDE_TITLE = [
    # energy, electrocatalysis, heterogeneous and materials chemistry
    r"\banodes?\b", r"\bcathodes?\b", r"photoanode", r"electrolytes?\b", r"electrocatalyst", r"electrode",
    r"\bco2 (reduction|electroreduction|conversion|hydrogenation|methanation)", r"reduction of co2", r"\bco2[- ]to[- ]",
    r"carbon dioxide (to|reduction|electroreduction)", r"\bco2 capture", r"oxygen (evolution|reduction)",
    r"hydrogen (evolution|production|storage|peroxide production)",
    r"water (oxidation|treatment|purification|harvesting|electrolysis)", r"nitrate[- ](to|electro)",
    r"electrocatalytic nitrate", r"nitrite (electro)?reduction", r"ammonia (synthesis|production|decomposition|electrosynthesis)",
    r"(electrochemical|green) ammonia", r"\burea (electro)?synthesis", r"(glycine|amino acids?|urea|oxime) (electro)?synthesis via",
    r"amino acids? electrosynthesis", r"h2o2 (production|synthesis)", r"semiconductor", r"heterojunction",
    r"(metal|lead) halide", r"\btio2\b", r"nanoparticle", r"nanosheet", r"nanocluster", r"nanocrystal", r"nanowire",
    r"nanotube", r"nanostruct", r"nanocube", r"nanopore", r"core-shell", r"polymers?\b", r"polymeric", r"copolymer",
    r"polyimide", r"polymeri[sz]ation", r"depolymeri", r"upcycling", r"polyethylene", r"polyolefin", r"plastics?\b",
    r"device", r"storage", r"capacitor", r"exfoliat", r"membranes?\b", r"\bfilms?\b", r"\bfibers?\b", r"coating",
    r"adhesive", r"\bgels?\b", r"ceramic", r"\balloys?\b", r"high-entropy", r"oxides? catalysts?",
    r"\b(metal|mixed|cerium|copper|iron|nickel|cobalt|zinc|titanium|tungsten|zirconium|manganese|vanadium|indium|tin|"
    r"aluminum|aluminium|molybdenum|lanthanoid) oxides?\b",
    r"\b(cuo|zno|ceo2|ceria|wo3|wox|moo3|nio|mno|fe2o3|fe3o4|co3o4|in2o3|sno2|al2o3|biobr)\b", r"electrochromic",
    r"hydrocracking", r"lignin", r"methane", r"syngas", r"furfural", r"\bhmf\b", r"volcano", r"oxidative dehydrogenation",
    r"dry reforming", r"plasma", r"(diatomic|dual-atom|single-site|bimetallic|adjacent metal|active|minority|metal) sites?\b",
    r"on-surface", r"\bon (au|ag|cu|pt|pd|ni|metal) surfaces?\b", r"electrochemical promotion", r"energy efficiency",
    r"current density", r"phase transition", r"magnet", r"spin (polari[sz]ation|crossover|state|coherence|qubit)",
    r"white phosphorus", r"\bp4\b", r"dinitrogen", r"\bn2 (reduction|fixation|activation|splitting)", r"pfas",
    r"degradation", r"recovery", r"biomass", r"\bpropane\b", r"(oxide|silica|alumina|carbon|zeolite)-supported",
    r"(electro)?synthesis of (hydrazine|ammonia|hydrogen peroxide|h2o2|urea|nitric acid|formic acid|formate|methanol)",
    r"electrocatalytic (biomass|glycerol|nitrate|co2|n2|oxygen|hydrogen)", r"semihydrogenation of (acetylene|alkynols?)",
    r"alkynol semihydrogenation", r"acetylene semihydrogenation", r"z-scheme", r"\bpoly\(", r"heptazine",
    r"\bco (reduction|electroreduction)", r"electrochemical co\b", r"atomically dispersed", r"glycerol",
    r"(electron|hole)/(electron|hole)", r"spin[- ]polari", r"pnictogen", r"\bgroup 1[3-7]\b",
    # biology, medicine, enzymology
    r"\bproteins?\b", r"proteom", r"\bin vivo\b", r"\bmice\b", r"\btumou?rs?\b", r"cancer (therapy|immunotherapy|treatment)",
    r"antibod", r"bacteri", r"antimicrobial", r"virus", r"pathogen", r"\bbiosynthesis\b",
    r"biosynthetic (gene|pathway|enzyme|machinery)", r"synthases?\b", r"lactamase", r"receptor", r"immun", r"inflammat",
    r"macrophage", r"neutrophil", r"signaling", r"phenotypic", r"direct-to-biology", r"profiling", r"prodrug",
    r"deep-tissue", r"\bliving (cells?|systems|organisms|animals)", r"genetically", r"kinase", r"\btargeting\b",
    r"\bmutations?\b", r"\batp\b",
    # spectroscopy, physics, analysis, computing
    r"imaging", r"\bprobes?\b", r"sensor", r"sensing", r"\bdetection\b", r"\bemitters?\b", r"\bemission\b",
    r"photophysic", r"excited-state dynamics", r"\bdynamics\b", r"ultrafast", r"single-molecule", r"diradical",
    r"two-photon", r"second-harmonic", r"kondo", r"molecular junction", r"conductance", r"machine learning",
    r"deep learning", r"neural", r"foundation model", r"artificial intelligence", r"language model", r"self-driving",
    r"autonomous lab", r"high-throughput experimentation", r"dataset", r"database", r"atmospheric", r"aerosol",
    r"combustion", r"prebiotic", r"interstellar", r"asteroid", r"stardust",
    # supramolecular, molecular machines
    r"crystal engineering", r"self-assembl", r"host-guest", r"molecular (machine|motor|switch)", r"photoswitch",
    r"rotaxane", r"foldamer", r"tweezer",
]
# SOFT exclusions (title only): drop, unless the title also has 2+ synthesis signals
# (so "Enantioselective synthesis of helical nanographenes" or "whole-cell biocatalytic cycloaddition" stay).
EXCLUDE_SOFT = [
    r"(?<!whole-)(?<!flow )\b(living |live-)?cells?\b", r"\bcellular\b", r"\bdna\b", r"\brna\b", r"graphene",
    r"carbon nitride", r"\bsurfaces?\b", r"interfac(e|es|ial)\b", r"\bclusters?\b", r"lattice", r"\bcages?\b",
    r"supramolecular", r"\bhosts?\b", r"chiroptical", r"circularly polarized", r"aggregation-induced", r"luminesc",
    r"phosphoresc", r"fluoresc", r"biomass", r"cellulose", r"adsorption", r"separation", r"heterogeneous",
    r"(?<!starting )(?<!raw )materials?\b", r"\bcomplexes\b", r"(oxo|nitride|nitrido|imido|hydride|carbide) complex",
    r"(dioxide|selenide) complex", r"electrosynthesis of (c\d|glycolic|acetic|adipic|ethylene|propylene)",
]
# Reviews, perspectives, corrections, editorials and similar non-research items.
NOT_RESEARCH = [
    r"^(a )?(review|minireview|perspective|outlook|editorial|essay|tutorial|primer|roadmap|viewpoint|commentary)\b",
    r"\b(recent|latest) (advances|progress|developments|trends|achievements)\b", r"^(advances|progress|developments|"
    r"trends|frontiers|perspectives|insights|opportunities|challenges) (in|on|of|and)\b", r"\ba (review|perspective)\b",
    r"\bstate of the art\b", r"^(correction|author correction|publisher correction|erratum|corrigendum|retraction|"
    r"expression of concern|addendum)\b", r"(front|back|inside|outside) cover", r"^(cover|masthead|table of contents|"
    r"news|research highlight|behind the science|introducing|meet the|author profile|obituary|in memoriam|"
    r"correspondence|reply|response to|comment on|frontispiece)\b",
]
WILEY_RESEARCH = {"COMMUNICATION", "RESEARCH ARTICLE"}            # other feed categories are skipped

TAGS = [("total synthesis", "Total synthesis"), ("natural product", "Natural products"),
        ("enantio", "Asymmetric catalysis"), ("asymmetric", "Asymmetric catalysis"),
        ("skeletal editing", "Skeletal editing"), ("dearomati", "Dearomatization"),
        ("photoredox", "Photoredox"), ("photocatal", "Photocatalysis"), ("electrochem", "Electrochemistry"),
        ("c-h", "C–H functionalization"), ("cross-coupling", "Cross-coupling"),
        ("cycloaddition", "Cycloaddition"), ("carbene", "Carbene chemistry"),
        ("radical", "Radical chemistry"), ("fluor", "Fluorine chemistry"), ("organocatal", "Organocatalysis")]

_SIG = [re.compile(p) for p in SIGNALS]
_MOTIF = [re.compile(p) for p in MOTIFS]
_EX_ANY = [re.compile(p) for p in EXCLUDE_ANY]
_EX_TITLE = [re.compile(p) for p in EXCLUDE_TITLE]
_EX_SOFT = [re.compile(p) for p in EXCLUDE_SOFT]
_NOT_RESEARCH = [re.compile(p) for p in NOT_RESEARCH]


def out_of_time():
    return time.time() > DEADLINE


def norm(s):
    # all dash-like characters -> "-" (incl. U+2500 "─", which Wiley uses for bonds: C─H)
    s = re.sub("[\u2010-\u2015\u2212\u2500\u2501\ufe63\uff0d]", "-", s or "").lower()
    s = re.sub(r"<[^>]+>", "", s)                      # titles: drop inline tags (C(<i>sp</i><sup>3</sup>) -> c(sp3))
    s = re.sub(r"\b(co|n|h|o|d|so|no|ch) ([2-4])\b", r"\1\2", s)   # "CO 2" -> "co2"
    return re.sub(r"\s+", " ", s).strip()


def hits(patterns, text):
    return [p.pattern for p in patterns if p.search(text)]


def judge(title, ab="", journal=""):
    """Return (keep: bool, reason: str). Reason explains rejections (shown in the log).
    Title points: 2 per reaction/strategy signal + 1 per organic structure word.
    Score: title points + abstract signals (max 3); must reach the journal's minimum."""
    t, a = norm(title), norm(ab)
    if hits(_NOT_RESEARCH, t):
        return False, "not a research paper"
    bad = hits(_EX_ANY, t + " " + a) or hits(_EX_TITLE, t)
    if bad:
        return False, "off-topic: " + bad[0]
    pts = 2 * len(hits(_SIG, t)) + len(hits(_MOTIF, t))
    if pts < 2:
        return False, "no synthesis signal in title"
    soft = hits(_EX_SOFT, t)
    if soft and pts < 4:
        return False, "off-topic: " + soft[0]
    score = pts + min(3, len(hits(_SIG, a)))
    need = MIN_SCORE.get(journal, 2)
    if score < need:
        return False, f"weak evidence ({score} < {need})"
    return True, f"score {score}"


def relevant(title, ab="", journal=""):
    return judge(title, ab, journal)[0]


# ---------------------------------------------------------------- HTTP
RETRYABLE = {429, 500, 502, 503, 504}


def _get(url, tries=3, headers=None, limit=None, quiet=()):
    """GET url -> (bytes or None, status). Retries only temporary problems (429, 5xx, network).
    403/404 and other client errors fail at once; every failure is logged with its URL,
    except status codes listed in `quiet` (expected 'not found' answers)."""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    hdrs.update(headers or {})
    for i in range(tries):
        try:
            with urlopen(Request(url, headers=hdrs), timeout=40) as r:
                return r.read(limit), 200
        except HTTPError as e:
            if e.code not in RETRYABLE or i == tries - 1:
                if e.code not in quiet:
                    print(f"  HTTP {e.code}: {url}", file=sys.stderr)
                return None, e.code
            wait = 2 * (i + 1)
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra and ra.isdigit():
                wait = max(wait, min(int(ra), 120))
            print(f"  HTTP {e.code}, retry {i + 1}: {url}", file=sys.stderr)
        except Exception as e:  # network error / timeout
            if i == tries - 1:
                print(f"  failed ({e}): {url}", file=sys.stderr)
                return None, 0
            wait = 2 * (i + 1)
            print(f"  network error, retry {i + 1}: {url}", file=sys.stderr)
        time.sleep(wait)
    return None, 0


def _json(raw):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def get_json(url, tries=3):
    return _json(_get(url, tries)[0])


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


INLINE_TAG = re.compile(r"</?(?:jats:)?(?:sub|sup|i|b|em|strong|italic|bold|sc|underline)\b[^>]*>", re.I)


def clean_abstract(a):
    a = INLINE_TAG.sub("", a or "")                  # CO<sub>2</sub> -> CO2, <i>N</i>- -> N-
    a = re.sub(r"<[^>]+>", " ", a)                   # paragraph/section tags -> space
    a = html.unescape(re.sub(r"\s+", " ", a)).strip()
    return re.sub(r"^abstract[:.\s]*", "", a, flags=re.I)


def openalex_abstract(doi):
    raw, _ = _get(f"https://api.openalex.org/works/https://doi.org/{quote(doi)}?mailto={MAILTO}"
                  "&select=abstract_inverted_index", quiet=(404,))   # 404 = not indexed yet (new papers)
    inv = (_json(raw) or {}).get("abstract_inverted_index")
    if not inv:
        return ""
    words = {}
    for w, pos in inv.items():
        for p in pos:
            words[p] = w
    return " ".join(words[i] for i in sorted(words))


# ---------------------------------------------------------------- Wiley (Angew. Chem.) via RSS
# Wiley article pages refuse automated requests (HTTP 403), but the journal's RSS feed is
# served normally and each item carries the TOC graphic URL, the full abstract and the
# article type. The feed lists the latest issue + EarlyView, so it covers papers found by the
# 2-day runs; older papers that have dropped out of the feed keep the placeholder.
WILEY_FEEDS = ["https://onlinelibrary.wiley.com/feed/15213773/most-recent"]   # Angew. Chem. Int. Ed.
FEED_NS = {"content": "http://purl.org/rss/1.0/modules/content/",
           "dc": "http://purl.org/dc/elements/1.1/",
           "prism": "http://prismstandard.org/namespaces/basic/2.0/"}
IMG_SRC = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
_wiley = None


def wiley_feed():
    """{doi: {"img", "ab", "type"}} from the Wiley feeds (fetched once per run)."""
    global _wiley
    if _wiley is None:
        _wiley = {}
        for url in WILEY_FEEDS:
            raw, _ = _get(url)
            if not raw:
                print("  Wiley feed unavailable; Angew. images/abstracts skipped this run", file=sys.stderr)
                continue
            try:
                root = ET.fromstring(raw)
            except ET.ParseError as e:
                print("  Wiley feed unreadable:", e, file=sys.stderr)
                continue
            for item in root.iter("item"):
                doi = (item.findtext("prism:doi", namespaces=FEED_NS)
                       or item.findtext("dc:identifier", namespaces=FEED_NS)
                       or item.findtext("guid") or "").strip().lower()
                if not doi:
                    continue
                body = item.findtext("content:encoded", namespaces=FEED_NS) or ""
                m = IMG_SRC.search(body)
                parts = re.split(r"<h2[^>]*>\s*abstract\s*</h2>", body, flags=re.I)
                _wiley[doi] = {"img": html.unescape(m.group(1)) if m else None,
                               "ab": clean_abstract(parts[1]) if len(parts) > 1 else "",
                               "type": (item.findtext("category") or "").strip().upper()}
        print(f"  Wiley feed: {len(_wiley)} recent items", flush=True)
    return _wiley


def wiley(doi):
    d = (doi or "").lower()
    return wiley_feed().get(d, {}) if d.startswith("10.1002/") else {}


# ---------------------------------------------------------------- TOC / graphical-abstract images
# We store only the image URL (hot-linked, shown with a credit and a link to the paper), never a copy.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
ACS_PREFIX = {"jacs": "ja", "acscatal": "cs"}
BAD_IMG = ("logo", "default", "placeholder", "favicon", "sprite", "cover", "social", "banner")
REFUSED = (401, 403, 429)
_refused = {}                                         # DOI prefix -> refusals in a row (this run)


def acs_image(doi):
    """ACS figure at a predictable CDN path (loaded by the visitor's browser,
    which tries .gif/.jpeg/.jpg/.png in turn, so no server request to ACS is needed)."""
    m = re.match(r"10\.1021/(jacs|acscatal)\.([0-9a-z]+)$", doi.lower())
    if not m:
        return None
    return f"https://pubs.acs.org/cms/10.1021/{doi.split('/', 1)[1]}/asset/images/medium/{ACS_PREFIX[m[1]]}{m[2]}_0001.gif"


def nature_image(doi):
    """Figure 1 of a Nature-family article, built from the DOI (no request to nature.com).
    10.1038/s44160-026-01162-w -> .../MediaObjects/44160_2026_1162_Fig1_HTML.png"""
    m = re.match(r"10\.1038/(s(\d{5})-(\d{3})-(\d+)-[0-9a-z])$", doi.lower())
    if not m:
        return None
    suffix, journal, yr, num = m.groups()
    return (f"https://media.springernature.com/lw685/springer-static/image/art%3A10.1038%2F{suffix}"
            f"/MediaObjects/{journal}_{2000 + int(yr)}_{int(num)}_Fig1_HTML.png")


def page_image(url):
    """Read the og:image / twitter:image of an article page. Returns (image url or None, HTTP status)."""
    raw, code = _get(url, tries=1, headers={"User-Agent": BROWSER_UA, "Accept": "text/html"}, limit=400000)
    if raw is None:
        return None, code
    page = raw.decode("utf-8", "ignore")
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, page, re.I)
        if m:
            u = html.unescape(m.group(1))
            if u.startswith("http") and not any(b in u.lower() for b in BAD_IMG):
                return u, code
    return None, code


def free_image(doi):
    """Image URL that needs no request to the publisher's article page (or None)."""
    d = (doi or "").lower()
    return acs_image(d) or nature_image(d) or (wiley(d).get("img") if d.startswith("10.1002/") else None)


def find_image(doi):
    d = doi.lower()
    img = free_image(d)
    if img or d.startswith(("10.1021/", "10.1038/", "10.1002/")):
        return img
    prefix = d.split("/", 1)[0]                        # Science (10.1126), Cell Press (10.1016)
    if _refused.get(prefix, 0) >= 3:                   # publisher keeps refusing: stop asking this run
        return None
    img, code = page_image("https://doi.org/" + doi)
    if code in REFUSED:
        _refused[prefix] = _refused.get(prefix, 0) + 1
        if _refused[prefix] == 3:
            print(f"  {prefix}: image pages refused 3 times in a row; skipped for the rest of this run",
                  file=sys.stderr)
    else:
        _refused[prefix] = 0
    return img


def fill_images(papers, retry=False):
    n = 0
    for p in papers:
        doi = p.get("doi")
        if not doi or p.get("img"):
            continue                                  # entries without a DOI: set "img" by hand
        img = free_image(doi)                         # ACS / Nature / Wiley feed: no page request
        if img:
            p["img"] = img
            n += 1
            continue
        if doi.lower().startswith(("10.1021/", "10.1038/", "10.1002/")) or ("img" in p and not retry):
            p.setdefault("img", None)
            continue
        if out_of_time():
            break
        p["img"] = find_image(doi)
        n += bool(p["img"])                           # count only images actually found
        time.sleep(0.6)
    return n


# ---------------------------------------------------------------- DOIs and full abstracts for existing entries
def crossref_abstract(doi):
    raw, code = _get(f"https://api.crossref.org/works/{quote(doi)}?mailto={MAILTO}", quiet=(404,))
    if code == 404:
        print(f"  DOI not found at Crossref; please check this entry in data/papers.json: {doi}",
              file=sys.stderr)
        return ""
    return clean_abstract(((_json(raw) or {}).get("message") or {}).get("abstract"))


def fill_abstracts(papers, retry=False):
    """Replace short summaries with the publisher's abstract (Wiley feed, then Crossref, then OpenAlex)."""
    n = 0
    for p in papers:
        doi = p.get("doi")
        if not doi or p.get("ab_full"):
            continue
        ab = wiley(doi).get("ab", "")
        if len(ab) <= 80:
            if "ab_full" in p and not retry:
                continue
            if out_of_time():
                break
            ab = crossref_abstract(doi) or openalex_abstract(doi)
            time.sleep(0.3)
        if len(ab) > 80:
            p["ab"], p["ab_full"] = ab, True
            n += 1                                    # count only abstracts actually found
        else:
            p["ab_full"] = False
    return n


def fill_doi(papers):
    """Find the DOI of entries that have none (matched by title, strict similarity)."""
    n = 0
    for p in papers:
        if p.get("doi"):
            continue
        if out_of_time():
            break
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


def fetch_journal(name, issns, since, known=frozenset(), blocked=frozenset()):
    seen, found, rejected = set(), [], 0
    for issn in issns:
        cursor = "*"
        for _ in range(MAX_PAGES):
            if out_of_time():
                print(f"  {name}: time budget reached, stopping here", file=sys.stderr)
                break
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
                if not doi or doi in seen or doi in known or doi in blocked or not title:
                    continue
                seen.add(doi)
                w = wiley(doi)
                if w.get("type") and w["type"] not in WILEY_RESEARCH:
                    continue                                   # review, cover, editorial... (Wiley feed)
                ok, why = judge(title, "", name)
                if not ok and not why.startswith("weak"):
                    rejected += 1
                    continue                                   # cheap title check first: no abstract lookups
                d = pub_date(it)
                if not d:
                    continue
                ab = clean_abstract(it.get("abstract")) or w.get("ab") or openalex_abstract(doi)
                ok, why = judge(title, ab, name)
                if not ok:
                    rejected += 1
                    continue
                low = norm(title + " " + ab)
                tag = next((lab for k, lab in TAGS if k in low), "Synthetic methodology")
                found.append({"date": d.isoformat(), "j": name, "t": title, "au": authors(it),
                              "tag": tag, "ab": ab or "Abstract available at the publisher (DOI link).",
                              "doi": it["DOI"], "ab_full": bool(ab), "img": find_image(it["DOI"])})
            cursor = msg.get("next-cursor")
            if not cursor:
                break
            time.sleep(0.3)
        else:
            print(f"  {name} ({issn}): stopped at {MAX_PAGES} pages; some papers may be missing",
                  file=sys.stderr)
    print(f"  kept {len(found)}, rejected {rejected}", flush=True)
    return found


def load_blocked():
    if not os.path.exists(BLOCKED):
        return set()
    with open(BLOCKED, encoding="utf-8") as f:
        return {ln.strip().lower().replace("https://doi.org/", "") for ln in f
                if ln.strip() and not ln.lstrip().startswith("#")}


def prune(by_key, blocked):
    """Remove saved papers that fail the topic filter (unless "keep": true) or are blocked."""
    removed = []
    for k, p in list(by_key.items()):
        if p.get("keep"):
            continue
        doi = (p.get("doi") or "").lower()
        if doi and doi in blocked:
            removed.append((p, "blocked.txt"))
        else:
            ab = p.get("ab", "")
            if ab.startswith("Abstract available at the publisher"):
                ab = ""
            ok, why = judge(p.get("t", ""), ab, p.get("j", ""))
            if ok:
                continue
            removed.append((p, why))
        del by_key[k]
    if removed:
        print(f"Removed {len(removed)} saved papers that are not organic synthesis "
              "(add \"keep\": true to an entry to protect it):")
        for p, why in removed[:200]:
            print(f"  - [{p.get('j', '')}] {p.get('t', '')[:110]}  ({why})")
    return len(removed)


def write(by_key):
    year = date.today().year                                         # current year only
    out = sorted((p for p in by_key.values() if p["date"].startswith(str(year))), key=lambda p: p["date"], reverse=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD (default: 10 days ago)")
    ap.add_argument("--images-only", action="store_true", help="skip paper search, only fill missing DOIs, abstracts, images")
    ap.add_argument("--retry-images", action="store_true", help="also retry papers whose image was not found")
    ap.add_argument("--retry-abstracts", action="store_true", help="also retry papers whose full abstract was not found")
    a = ap.parse_args()
    since = a.since or (date.today() - timedelta(days=10)).isoformat()
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            existing = json.load(f)
    else:
        existing = []
    by_key = {(p.get("doi") or p["t"]).lower(): p for p in existing}   # existing entries (and manual edits) win
    blocked = load_blocked()
    pruned = prune(by_key, blocked)
    known = {(p.get("doi") or "").lower() for p in existing}
    added = 0
    if not a.images_only:
        for name, issns, _ in JOURNALS:
            if out_of_time():
                break
            print(f"{name} …", flush=True)
            for p in fetch_journal(name, issns, since, known, blocked):
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
    with_img = sum(1 for p in out if p.get("img"))
    print(f"added {added}, removed {pruned}, DOIs found {nd}, abstracts filled {na}, images filled {ni}; "
          f"total {len(out)} papers, {with_img} with an image → {OUT}")
    if out_of_time():
        print(f"Time budget reached before the end. Run the workflow again with the same start date ({since}) "
              "to continue; papers already saved are skipped.")


if __name__ == "__main__":
    main()
