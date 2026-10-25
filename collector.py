#!/usr/bin/env python3
"""Ngai Group Archive collector.

Pulls recent papers from the tracked journals (Crossref), keeps only organic-synthesis
papers (new reactions and catalytic methods, total synthesis, skeletal editing, etc.),
and merges them into data/papers.json.
Run every 2 days (see .github/workflows/update.yml).

  python collector.py                      # last 10 days
  python collector.py --since 2026-01-01   # backfill the year

Version 9 (25 Oct 2026):
- Much stricter topic filter. A paper is kept only if its TITLE names a synthetic reaction,
  method or strategy (see SIGNALS), and nothing in it points to materials, energy,
  biology, spectroscopy, reviews, etc. (see EXCLUDE_ANY / EXCLUDE_TITLE).
  Papers already in data/papers.json that fail the filter are removed on the next run
  (listed in the log). Add  "keep": true  to an entry to protect it, and list DOIs in
  data/blocked.txt (one per line) to remove a paper for good.
- Nature-family images are built from the DOI (Figure 1 on media.springernature.com),
  so no request to nature.com is needed.
- JACS / ACS Catal.: the real TOC graphic is read from the ACS RSS feeds (ASAP + current issue),
  or from the article page for older papers (max ACS_PAGE_LIMIT per run). Figure 1 is only a last resort.
- Only the best PER_MONTH (default 120) papers of each month are kept, ranked by how clearly they
  are synthesis papers (plus journal weight); one topic may fill at most 40% of a month.
- Chem (Cell Press): graphical abstract linked from Elsevier's image server, built from the PII.
- TOC picture helper (toc-helper.html): a bookmark that collects ACS / Wiley / Science TOC graphics in your
  own browser; upload its toc-....json file to data/inbox and the next run unpacks it into data/toc.
  data/need_toc.json lists the papers still waiting for a picture.
- Image search (Brave Search API, BRAVE_API_KEY secret): papers that still have no picture are searched by
  title; a result is used only if its page carries the paper's DOI or title. Up to IMAGE_SEARCH_LIMIT (60) per run; papers under 30 days old are retried every run (up to 10 times),
  older misses after 30 days (3 tries); a shorter second query (first words + journal) is tried when the full title finds nothing.
  If the original picture's site refuses servers, Brave's own 500-px thumbnail of it is shown (link only).
- SAVE_TOC=1 (workflow setting) keeps a copy of each TOC graphic in data/toc/ so it displays
  even if the publisher blocks its images on other websites.
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

# The site keeps the best PER_MONTH papers of each month. Change the number here,
# or set PER_MONTH in the workflow.
PER_MONTH = int(os.environ.get("PER_MONTH") or 120)
TOPIC_SHARE = 0.4               # one topic (e.g. asymmetric catalysis) may fill at most 40% of a month
# Extra ranking weight by journal (how prominent a paper is, besides how clearly it is synthesis).
JOURNAL_WEIGHT = {"Nature": 5, "Science": 5, "Nature Chemistry": 4, "Nature Synthesis": 4, "Nature Catalysis": 2,
                  "J. Am. Chem. Soc.": 1.5, "Angew. Chem. Int. Ed.": 1, "Chem": 1, "ACS Catal.": 0.5,
                  "Nature Communications": 0.5}
SKIP_DOI = ("10.1002/ange.", "10.26434/")   # German edition of Angewandte (duplicate), ChemRxiv preprints

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
    r"\bnano(?!graphene|belt|hoop|ring|tube)[a-z]+", r"surfactant", r"isobutane", r"interfacial engineering",
    r"\bgrowth\b", r"soot", r"\b(ms-)?caspt2\b", r"\bdft (calculations?|study|studies|investigation)", r"\bab initio\b",
    r"insights? from .*(calculations|computations|simulations)", r"\bcomputational (study|studies|investigation|insights?|analysis)",
    r"\btheoretical (study|studies|investigation|insights?)",
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
    r"^(addition|additions and corrections|correction|retraction|erratum) to\b", r"\brecent applications\b",
    r":.*\b(outlook|perspectives?|overview|a review)\b\.?$",
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


def rank(p):
    """How strongly a paper is an organic-synthesis highlight (used to pick the best PER_MONTH of each month)."""
    t = norm(p.get("t", ""))
    ab = p.get("ab", "")
    if ab.startswith("Abstract available at the publisher"):
        ab = ""
    a = norm(ab)
    off_topic_words = len(hits(_EX_SOFT, a)) + len(hits(_EX_TITLE, a))   # materials/biology words in the abstract
    return (2 * min(len(hits(_SIG, t)), 4) + min(len(hits(_MOTIF, t)), 2) + 0.6 * min(len(hits(_SIG, a)), 8)
            - 1.5 * off_topic_words + JOURNAL_WEIGHT.get(p.get("j", ""), 0)
            + (2 if "total synthes" in t else 0) - (3 if not ab else 0))


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
# By default only the image URL is stored (hot-linked, shown with a credit and a link to the paper).
# With SAVE_TOC=1 (set in the workflow) a copy is also kept in data/toc/ and served from your own site,
# which keeps working when a publisher does not let other websites display its images.
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
ACS_PREFIX = {"jacs": "ja", "acscatal": "cs"}
BAD_IMG = ("logo", "default", "placeholder", "favicon", "sprite", "cover", "social", "banner")
REFUSED = (401, 403, 429)
_refused = {}                                         # DOI prefix / host -> refusals in a row (this run)
SAVE_TOC = os.environ.get("SAVE_TOC", "").strip().lower() in ("1", "true", "yes")
TOC_DIR = os.path.join(HERE, "data", "toc")
INBOX = os.path.join(HERE, "data", "inbox")           # files made by the TOC helper bookmark go here
NEED = os.path.join(HERE, "data", "need_toc.json")    # list the TOC helper reads: papers still without a picture
HELPER_PREFIXES = ("10.1021/", "10.1002/", "10.1126/")  # ACS, Wiley, Science: publishers that block servers
# Image search (Brave Search API, the official alternative to Google image search): finds the TOC graphic by
# the paper title for papers that still have no picture. Needs the BRAVE_API_KEY secret; skipped without it.
BRAVE_KEY = (os.environ.get("BRAVE_API_KEY") or "").strip()
try:                                                                     # per run; 15 runs/month ≈ 900, inside the free credit
    IMAGE_SEARCH_LIMIT = max(0, min(2000, int(os.environ.get("IMAGE_SEARCH_LIMIT") or 60)))   # searches (API calls) per run
except ValueError:
    IMAGE_SEARCH_LIMIT = 60
IMAGE_SEARCH_RETRY_DAYS = 30                                             # older papers with no good result: retried after 30 days
IMAGE_SEARCH_MAX_TRIES = 3                                               # ... at most 3 times in total
RECENT_DAYS = 30                                                         # papers newer than this (not yet indexed everywhere):
RECENT_RETRY_DAYS = 2                                                    # ... retried every run (2 days)
RECENT_MAX_TRIES = 10                                                    # ... up to 10 times
SEARCH_VERSION = 3                                                       # papers missed by an older version are retried
SEARCH_LOG = os.path.join(HERE, "data", "search_log.json")              # what the search returned for misses (for tuning)
JOURNAL_SHORT = {"J. Am. Chem. Soc.": "JACS", "ACS Catal.": "ACS Catalysis", "Angew. Chem. Int. Ed.": "Angewandte Chemie",
                 "Science": "Science", "Chem": "Chem"}
_search_calls = 0
_search_misses = []
BRAVE_THUMB = "imgs.search.brave.com"                                    # Brave's image server (search-result thumbnails)
BAD_SEARCH_IMG = ("logo", "cover", "avatar", "profile", "portrait", "headshot", "icon", "banner", "favicon",
                  "author", "people", "staff", "team", "badge", "sprite", "placeholder")
ACS_PAGE_LIMIT = 150                                  # ACS article-page lookups per run (older papers)
_acs_pages = 0

# ACS journal RSS feeds (ASAP + current issue): every item carries its TOC graphic.
ACS_FEEDS = [
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=jacsat",   # JACS, ASAP
    "https://pubs.acs.org/action/showFeed?type=etoc&feed=rss&jc=jacsat",     # JACS, current issue
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=accacs",   # ACS Catal., ASAP
    "https://pubs.acs.org/action/showFeed?type=etoc&feed=rss&jc=accacs",     # ACS Catal., current issue
]
ACS_DOI = re.compile(r"10\.1021/[a-z0-9.\-]*[a-z0-9]", re.I)
_acs = None


def acs_feed():
    """{doi: TOC graphic URL} from the ACS feeds (fetched once per run)."""
    global _acs
    if _acs is None:
        _acs = {}
        for url in ACS_FEEDS:
            raw, code = _get(url, headers={"Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8"})
            if not raw:
                print(f"  ACS feed unavailable (HTTP {code}): {url}", file=sys.stderr)
                continue
            try:
                root = ET.fromstring(raw)
            except ET.ParseError as e:
                print("  ACS feed unreadable:", e, file=sys.stderr)
                continue
            for item in (el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "item"):
                link = next((ch.text or "" for ch in item if ch.tag.rsplit("}", 1)[-1] == "link"), "")
                text = " ".join(t for t in item.itertext())
                m = ACS_DOI.search(link) or ACS_DOI.search(text)
                img = next((html.unescape(u) for u in IMG_SRC.findall(text) if "/asset/images/" in u), None)
                if m and img:
                    _acs[m.group(0).lower()] = ("https://pubs.acs.org" + img) if img.startswith("/") else img
        print(f"  ACS feeds: TOC graphics for {len(_acs)} recent papers", flush=True)
    return _acs


def acs_guess(doi):
    """Last resort for ACS: Figure/Scheme 1 at its predictable path (the TOC graphic's number varies)."""
    m = re.match(r"10\.1021/(jacs|acscatal)\.([0-9a-z]+)$", doi.lower())
    if not m:
        return None
    return f"https://pubs.acs.org/cms/10.1021/{doi.split('/', 1)[1]}/asset/images/medium/{ACS_PREFIX[m[1]]}{m[2]}_0001.gif"


acs_image = acs_guess                                 # old name, kept for compatibility


def is_acs_guess(url):
    return bool(url) and "pubs.acs.org/cms/" in url and url.endswith("_0001.gif")


def nature_image(doi):
    """Figure 1 of a Nature-family article, built from the DOI (no request to nature.com).
    10.1038/s44160-026-01162-w -> .../MediaObjects/44160_2026_1162_Fig1_HTML.png"""
    m = re.match(r"10\.1038/(s(\d{5})-(\d{3})-(\d+)-[0-9a-z])$", doi.lower())
    if not m:
        return None
    suffix, journal, yr, num = m.groups()
    return (f"https://media.springernature.com/lw685/springer-static/image/art%3A10.1038%2F{suffix}"
            f"/MediaObjects/{journal}_{2000 + int(yr)}_{int(num)}_Fig1_HTML.png")


def page_image(url, must=None):
    """Read the og:image / twitter:image of an article page. Returns (image url or None, HTTP status).
    `must`: a substring the image URL has to contain (e.g. "/asset/images/" for the ACS TOC graphic)."""
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
            if u.startswith("/"):
                u = "https://" + url.split("/")[2] + u
            if u.startswith("http") and not any(b in u.lower() for b in BAD_IMG) and (not must or must in u):
                return u, code
    return None, code


def _note_refusal(key, code, what):
    if code in REFUSED:
        _refused[key] = _refused.get(key, 0) + 1
        if _refused[key] == 3:
            print(f"  {what}: refused 3 times in a row; skipped for the rest of this run", file=sys.stderr)
    elif code == 200:
        _refused[key] = 0


def elsevier_image(p):
    """Cell Press / Elsevier graphical abstract, built from the article's PII (Crossref "alternative-id").
    Only the link is stored; the visitor's browser loads it (Elsevier asks robots not to download)."""
    doi = (p.get("doi") or "").lower()
    if not doi.startswith("10.1016/"):
        return None
    pii = p.get("pii") or ""
    if not pii and not out_of_time():
        j = get_json(f"https://api.crossref.org/works/{quote(doi)}?mailto={MAILTO}")
        alt = ((j or {}).get("message") or {}).get("alternative-id") or []
        pii = next((a for a in alt if re.match(r"^S\d{4}", a)), "")
        p["pii"] = pii
        time.sleep(0.3)
    pii = re.sub(r"[^A-Za-z0-9]", "", pii)
    return f"https://ars.els-cdn.com/content/image/1-s2.0-{pii}-fx1.jpg" if pii else None


def free_image(doi):
    """Image URL that needs no request to the publisher's article page (or None)."""
    d = (doi or "").lower()
    if d.startswith("10.1038/"):
        return nature_image(d)
    if d.startswith("10.1002/"):
        return wiley(d).get("img")
    if d.startswith("10.1021/"):
        return acs_feed().get(d)
    return None


def acs_page_toc(doi):
    """TOC graphic from the ACS article page (og:image), within the per-run limit."""
    global _acs_pages
    if _refused.get("10.1021", 0) >= 3 or _acs_pages >= ACS_PAGE_LIMIT or out_of_time():
        return None
    _acs_pages += 1
    img, code = page_image("https://pubs.acs.org/doi/" + doi, must="/asset/images/")
    _note_refusal("10.1021", code, "ACS article pages")
    time.sleep(1.0)
    return img


def find_image(doi):
    d = doi.lower()
    img = free_image(d)
    if img:
        return img
    if d.startswith("10.1021/"):                      # ACS: article page, else Figure 1 as a last resort
        return acs_page_toc(doi) or acs_guess(doi)
    if d.startswith(("10.1038/", "10.1002/")):
        return None
    prefix = d.split("/", 1)[0]                        # Science (10.1126), Cell Press (10.1016)
    if _refused.get(prefix, 0) >= 3:                   # publisher keeps refusing: stop asking this run
        return None
    img, code = page_image("https://doi.org/" + doi)
    _note_refusal(prefix, code, f"{prefix} image pages")
    return img


IMAGE_MAGIC = (b"GIF87a", b"GIF89a", b"\x89PNG", b"\xff\xd8\xff")


def save_copy(p):
    """SAVE_TOC=1: keep a copy of the image in data/toc/ and point the entry at it."""
    url = p.get("img") or ""
    if not url.startswith("http") or "springernature.com" in url or "els-cdn.com" in url or BRAVE_THUMB in url:   # links
        return False
    host = url.split("/")[2]
    if _refused.get("save:" + host, 0) >= 3 or out_of_time():
        return False
    base = os.path.basename(url.split("?")[0]) or "image"
    name = re.sub(r"[^a-z0-9._-]+", "_", p["doi"].lower() + "__" + base.lower())[:150]
    dest = os.path.join(TOC_DIR, name)
    if not os.path.exists(dest):
        raw, code = _get(url, tries=2, headers={"User-Agent": BROWSER_UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
                         limit=5_000_000, quiet=(404,))
        if not raw or not (raw.startswith(IMAGE_MAGIC) or raw[8:12] == b"WEBP"):
            _note_refusal("save:" + host, code if raw is None else 403, f"image downloads from {host}")
            return False
        _note_refusal("save:" + host, 200, "")
        os.makedirs(TOC_DIR, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(raw)
        time.sleep(0.5)
    p["img_src"], p["img"] = url, "data/toc/" + name
    return True


def fill_images(papers, retry=False):
    """Fill missing images, replace ACS Figure-1 guesses by the real TOC graphic, and (SAVE_TOC=1) keep copies."""
    n = 0
    for p in papers:
        doi = p.get("doi")
        if not doi:
            continue                                  # entries without a DOI: set "img" by hand
        d = doi.lower()
        current = p.get("img_src") or p.get("img")
        if current and not is_acs_guess(current):
            if SAVE_TOC:
                save_copy(p)
            continue
        img = free_image(d) or elsevier_image(p)      # Nature / Wiley feed / ACS feed / Elsevier: no page request
        if not img and d.startswith("10.1021/"):
            img = acs_page_toc(doi)
        elif not img and not d.startswith(("10.1038/", "10.1002/", "10.1016/")) and ("img" not in p or retry) and not out_of_time():
            img = find_image(doi)
            time.sleep(0.6)
        if img:
            if (p.get("img") or "").startswith("data/toc/"):          # replace an old saved Figure-1 copy
                try:
                    os.remove(os.path.join(HERE, p["img"]))
                except OSError:
                    pass
            p.pop("img_src", None)
            p["img"] = img
            n += 1
        elif not current:
            p["img"] = acs_guess(doi) if d.startswith("10.1021/") else None
        if SAVE_TOC and p.get("img"):
            save_copy(p)
    return n


# ---------------------------------------------------------------- DOIs and full abstracts for existing entries
def crossref_abstract(doi):
    raw, code = _get(f"https://api.crossref.org/works/{quote(doi)}?mailto={MAILTO}", quiet=(404,))
    if code == 404:
        print(f"  DOI not found at Crossref; please check this entry in data/papers.json: {doi}",
              file=sys.stderr)
        return ""
    return clean_abstract(((_json(raw) or {}).get("message") or {}).get("abstract"))


def starts_mid_sentence(ab):
    return bool(ab) and (ab[0].islower() or ab[0] in "-\u2010\u2013,;:).]")


def fill_abstracts(papers, retry=False):
    """Replace short summaries with the publisher's abstract (Wiley feed, then Crossref, then OpenAlex).
    An abstract that arrived without its first words is re-fetched once from Crossref, else marked with "…"."""
    n = 0
    for p in papers:
        doi = p.get("doi")
        if doi and p.get("ab_full") and starts_mid_sentence(p.get("ab", "")) and not out_of_time():
            better = crossref_abstract(doi)
            time.sleep(0.3)
            p["ab"] = better if len(better) > 80 and not starts_mid_sentence(better) else "… " + p["ab"]
            continue
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
            p["ab"], p["ab_full"] = ("… " + ab if starts_mid_sentence(ab) else ab), True
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
                           "select": "DOI,title,author,abstract,published-online,published-print,issued,alternative-id",
                           "rows": 100, "cursor": cursor, "mailto": MAILTO})
            j = get_json("https://api.crossref.org/works?" + q)
            msg = (j or {}).get("message", {})
            items = msg.get("items", [])
            if not items:
                break
            for it in items:
                doi = it.get("DOI", "").lower()
                title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", (it.get("title") or [""])[0])).strip()
                if not doi or doi in seen or doi in known or doi in blocked or not title or doi.startswith(SKIP_DOI):
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
                              "doi": it["DOI"], "ab_full": bool(ab)})   # image: looked up after selection
                if doi.startswith("10.1016/"):
                    found[-1]["pii"] = next((x for x in it.get("alternative-id") or [] if re.match(r"^S\d{4}", x)), "")
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
        elif doi.startswith(SKIP_DOI):
            removed.append((p, "duplicate edition or preprint"))
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


def select(by_key):
    """Keep the best PER_MONTH papers of each month (ranked by rank()).
    Entries with "keep": true always stay (and count toward their month)."""
    year = str(date.today().year)
    ranked = sorted((p for p in by_key.values() if p["date"].startswith(year)), key=lambda p: (-rank(p), p["date"]))
    unique, seen_titles = [], set()
    for p in ranked:                                   # the same paper under two DOIs: keep the better one
        t = norm(p.get("t", ""))
        if t not in seen_titles:
            seen_titles.add(t)
            unique.append(p)
    months = sorted({p["date"][:7] for p in unique})
    max_topic = max(1, int(PER_MONTH * TOPIC_SHARE))
    chosen, per_month = set(), {}
    for m in months:
        pool = [p for p in unique if p["date"][:7] == m]
        picked = [p for p in pool if p.get("keep")]
        topics = {}
        for p in picked:
            topics[p.get("tag")] = topics.get(p.get("tag"), 0) + 1
        for strict in (True, False):                   # 2nd pass fills up if the topic limit left gaps
            for p in pool:
                if len(picked) >= PER_MONTH:
                    break
                if p in picked or (strict and topics.get(p.get("tag"), 0) >= max_topic):
                    continue
                picked.append(p)
                topics[p.get("tag")] = topics.get(p.get("tag"), 0) + 1
        chosen.update(id(p) for p in picked)
        per_month[m] = len(picked)
    removed = 0
    for k, p in list(by_key.items()):
        if id(p) in chosen:
            p["score"] = round(rank(p), 1)
        elif p["date"].startswith(year) or not p.get("keep"):
            del by_key[k]
            removed += 1
    print(f"Kept the best {PER_MONTH} papers per month ({sum(per_month.values())} in total; "
          + ", ".join(f"{m[5:]}: {n}" for m, n in per_month.items()) + f"); {removed} lower-ranked papers left out.")
    return removed


def ensure_inbox():
    os.makedirs(INBOX, exist_ok=True)
    readme = os.path.join(INBOX, "README.txt")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as f:
            f.write("Upload the toc-....json files made by the 'Get TOC pictures' bookmark here.\n"
                    "The next run unpacks the pictures into data/toc and removes the file.\n")


def apply_inbox(by_key):
    """Unpack pictures made by the TOC helper bookmark (data/inbox/*.json, or toc-*.json uploaded to the top folder)."""
    import base64, glob
    files = sorted(glob.glob(os.path.join(INBOX, "*.json")) + glob.glob(os.path.join(HERE, "toc-*.json")))
    if not files:
        return 0
    by_doi = {(p.get("doi") or "").lower(): p for p in by_key.values() if p.get("doi")}
    added = skipped = 0
    for fn in files:
        try:
            with open(fn, encoding="utf-8") as f:
                images = (json.load(f) or {}).get("images") or {}
        except (OSError, ValueError) as e:
            print(f"  could not read {os.path.basename(fn)}: {e}", file=sys.stderr)
            continue
        for doi, data in images.items():
            p = by_doi.get(doi.lower())
            m = re.match(r"data:image/(jpeg|jpg|png|gif|webp);base64,(.+)$", data or "", re.S)
            if not p or not m:
                skipped += 1
                continue
            try:
                raw = base64.b64decode(m.group(2), validate=False)
            except ValueError:
                skipped += 1
                continue
            if not (raw.startswith(IMAGE_MAGIC) or raw[8:12] == b"WEBP"):
                skipped += 1
                continue
            ext = {"jpeg": ".jpg", "jpg": ".jpg"}.get(m.group(1), "." + m.group(1))
            name = re.sub(r"[^a-z0-9._-]+", "_", doi.lower())[:140] + "__toc" + ext
            os.makedirs(TOC_DIR, exist_ok=True)
            with open(os.path.join(TOC_DIR, name), "wb") as f:
                f.write(raw)
            p["img"], p["img_src"] = "data/toc/" + name, "TOC helper"
            added += 1
        os.remove(fn)                                  # processed: the workflow commits the removal
    print(f"TOC helper: {added} pictures added from {len(files)} file(s)"
          + (f"; {skipped} skipped (paper no longer in the archive, or not a picture)" if skipped else ""))
    return added


def cleanup_toc(by_key):
    """Delete saved pictures that no paper uses any more (e.g. papers that left the monthly top 100)."""
    if not os.path.isdir(TOC_DIR):
        return 0
    used = {os.path.basename(p["img"]) for p in by_key.values() if (p.get("img") or "").startswith("data/toc/")}
    n = 0
    for f in os.listdir(TOC_DIR):
        if f not in used:
            os.remove(os.path.join(TOC_DIR, f))
            n += 1
    return n


def shrink_image(raw):
    """Re-save large pictures as JPEG, at most 1100 px wide (keeps the site small). Needs Pillow; else unchanged."""
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(raw))
        im.load()
        if im.width <= 1100 and len(raw) <= 400_000:
            return raw, None
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        if im.width > 1100:
            im = im.resize((1100, round(im.height * 1100 / im.width)))
        out = _io.BytesIO()
        im.save(out, "JPEG", quality=86)
        return out.getvalue(), ".jpg"
    except Exception:
        return raw, None


def _brave(q):
    """One Brave image-search call. Returns the result list, or None if the key/allowance stops further searches."""
    global BRAVE_KEY, _search_calls
    _search_calls += 1
    url = "https://api.search.brave.com/res/v1/images/search?" + urlencode(
        {"q": q, "count": 20, "safesearch": "strict", "spellcheck": "false"})
    raw, code = _get(url, tries=2, headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_KEY})
    if code in (401, 403, 422):
        print(f"Image search: the BRAVE_API_KEY was refused (HTTP {code}); image search switched off for this run.",
              file=sys.stderr)
        BRAVE_KEY = ""
        return None
    if code in (402, 429):
        print("Image search: the free allowance (or your usage limit) is used up; the rest waits for the next run.",
              file=sys.stderr)
        BRAVE_KEY = ""
        return None
    return (_json(raw) or {}).get("results") or []


def _candidates(p, results):
    doi = (p.get("doi") or "").lower()
    tail = doi.split("/", 1)[-1]
    want = norm(p.get("t", ""))
    cands = []
    for r in results:
        img = ((r.get("properties") or {}).get("url") or "").strip()
        page = (r.get("url") or "").strip()
        title = norm(r.get("title") or "")
        if not img.startswith("http") or any(b in img.lower() for b in BAD_SEARCH_IMG):
            continue
        w = (r.get("properties") or {}).get("width") or (r.get("thumbnail") or {}).get("width") or 0
        if w and w < 250:
            continue
        doi_hit = bool(tail) and (tail in page.lower() or tail in img.lower() or tail.replace(".", "") in img.lower())
        sim = difflib.SequenceMatcher(None, title[:len(want) + 40], want).ratio() if title else 0
        if not (doi_hit or sim >= 0.72 or (len(want) > 30 and want[:70] in title)):
            continue
        score = (3 if doi_hit else 0) + sim
        if re.search(r"toc|abstract|-gra-\d|fx1|graphic|[-_]fa\.", img, re.I):
            score += 1.5                                # named as a TOC / graphical abstract
        m = re.search(r"/asset/images/.*_(\d{4})\.(gif|jpe?g|png)$", img, re.I)
        if m and "pubs.acs.org" in img:
            score += 1 + int(m.group(1)) / 100          # ACS numbers the TOC graphic last: prefer the highest number
        thumb = ((r.get("thumbnail") or {}).get("src") or "").strip()
        cands.append((score, img, page, thumb))
    cands.sort(key=lambda c: -c[0])
    return cands


def _host(u):
    return (u or "").split("/")[2] if (u or "").count("/") >= 2 else ""


def image_search(p):
    """Search the paper title in Brave image search; return (image bytes, ext, source page), (None, thumbnail
    link, source page) or None. Tries the full title, then (if nothing usable) the first words + the journal.
    A result is accepted only if its page carries the paper's DOI or the same title; logos, covers,
    portraits and tiny images are rejected."""
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", p.get("t", ""))).strip()
    words = title.split()
    queries = [title[:350]]
    if len(words) > 9:
        queries.append(" ".join(words[:9]) + " " + JOURNAL_SHORT.get(p.get("j", ""), ""))
    cands, seen = [], []
    for q in queries:
        results = _brave(q.strip())
        if results is None:                            # key refused / allowance used up
            return None
        seen += results[:6]
        cands = _candidates(p, results)
        if cands:
            break
    for _, img, page, _thumb in cands[:4]:
        data, code = _get(img, tries=1, headers={"User-Agent": BROWSER_UA, "Accept": "image/avif,image/webp,image/*"},
                          limit=8_000_000, quiet=(401, 403, 404, 429))
        if data and len(data) > 3000 and (data.startswith(IMAGE_MAGIC) or data[8:12] == b"WEBP"):
            data, ext = shrink_image(data)
            if not ext:
                ext = ".png" if data.startswith(b"\x89PNG") else ".gif" if data.startswith(b"GIF") else \
                      ".webp" if data[8:12] == b"WEBP" else ".jpg"
            return data, ext, page
        time.sleep(0.3)
    # The original sits on a site that refuses servers (ACS, Wiley...): show Brave's own thumbnail of it instead.
    for _, img, page, thumb in cands:
        if thumb.startswith("https://") and BRAVE_THUMB in thumb:
            return None, thumb, page
    _search_misses.append({"doi": p.get("doi"), "date": p.get("date"), "t": title[:160],
                           "top": [{"title": (r.get("title") or "")[:120], "page": _host(r.get("url")),
                                    "img": _host((r.get("properties") or {}).get("url"))} for r in seen[:6]]})
    return None


def write_search_log():
    """Keep the latest misses (what the search returned) in data/search_log.json, so the matching can be tuned."""
    if not _search_misses:
        return
    try:
        with open(SEARCH_LOG, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = []
    keep = {m["doi"]: m for m in old + _search_misses}            # newest entry per paper wins
    os.makedirs(os.path.dirname(SEARCH_LOG), exist_ok=True)
    with open(SEARCH_LOG, "w", encoding="utf-8") as f:
        json.dump(list(keep.values())[-400:], f, ensure_ascii=False, indent=0)


def fill_from_image_search(papers):
    """Pictures for papers that still have none, found by title in image search (BRAVE_API_KEY)."""
    if not BRAVE_KEY:
        return 0
    today = date.today()
    todo = []
    for p in sorted(papers, key=lambda p: p["date"], reverse=True):     # newest papers first
        img = p.get("img") or ""
        if not p.get("doi") or img.startswith("data/toc/") or "springernature.com" in img or BRAVE_THUMB in img:
            continue
        if img and not p["doi"].lower().startswith(HELPER_PREFIXES) and not is_acs_guess(img):
            continue                                   # e.g. Chem: its graphical abstract link is already set
        if p.get("search_v") == SEARCH_VERSION:
            try:
                age = (today - date.fromisoformat(p["date"])).days
            except ValueError:
                age = 999
            wait, tries = (RECENT_RETRY_DAYS, RECENT_MAX_TRIES) if age <= RECENT_DAYS else (IMAGE_SEARCH_RETRY_DAYS, IMAGE_SEARCH_MAX_TRIES)
            tried = p.get("search_tried")
            if tried and (today - date.fromisoformat(tried)).days < wait:
                continue
            if p.get("search_n", 0) >= tries:
                continue
        else:
            p["search_n"] = 0                          # missed by an older search version: try again now
        todo.append(p)
    found = tried_n = 0
    start_calls = _search_calls
    for p in todo:
        if not BRAVE_KEY or out_of_time() or _search_calls - start_calls >= IMAGE_SEARCH_LIMIT:
            break
        tried_n += 1
        p["search_tried"] = today.isoformat()
        p["search_n"] = p.get("search_n", 0) + 1
        p["search_v"] = SEARCH_VERSION
        hit = image_search(p)
        if hit and hit[0] is None:                     # Brave's thumbnail, shown as a link (no copy kept)
            p["img"], p["img_src"] = hit[1], hit[2] or "image search"
            for k in ("search_tried", "search_n", "search_v"):
                p.pop(k, None)
            found += 1
        elif hit:
            data, ext, page = hit
            os.makedirs(TOC_DIR, exist_ok=True)
            name = re.sub(r"[^a-z0-9._-]+", "_", p["doi"].lower())[:140] + "__search" + ext
            with open(os.path.join(TOC_DIR, name), "wb") as f:
                f.write(data)
            p["img"], p["img_src"] = "data/toc/" + name, page or "image search"
            for k in ("search_tried", "search_n", "search_v"):
                p.pop(k, None)
            found += 1
        time.sleep(1.1)                                # at most 1 search per second (free keys allow 1/second)
    left = max(0, len(todo) - tried_n)
    print(f"Image search: {found} TOC pictures found for {tried_n} papers ({_search_calls - start_calls} searches)"
          + (f"; {left} papers wait for the next runs" if left else ""))
    write_search_log()
    return found


def needs_picture(p):
    img = p.get("img") or ""
    return (p.get("doi") or "").lower().startswith(HELPER_PREFIXES) and not img.startswith("data/toc/")


def write_need(out):
    """List for the TOC helper: ACS / Wiley / Science papers that still have no saved picture."""
    need = [{"doi": p["doi"], "j": p["j"], "t": p["t"][:160], "m": p["date"][:7]} for p in out if needs_picture(p)]
    with open(NEED, "w", encoding="utf-8") as f:
        json.dump(need, f, ensure_ascii=False, indent=0)
    by_j = {}
    for x in need:
        by_j[x["j"]] = by_j.get(x["j"], 0) + 1
    print(f"Still without a TOC picture (for the TOC helper): {len(need)}"
          + (" — " + ", ".join(f"{j} {n}" for j, n in sorted(by_j.items(), key=lambda x: -x[1])) if need else ""))
    return len(need)


def write(by_key):
    year = date.today().year                                         # current year only
    out = sorted((p for p in by_key.values() if p["date"].startswith(str(year))), key=lambda p: p["date"], reverse=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


VERSION = "Ngai Group Archive collector, version 9 (25 Oct 2026)"


def main():
    print(VERSION, flush=True)
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
    left_out = select(by_key)                                        # best PER_MONTH papers of each month
    ensure_inbox()
    apply_inbox(by_key)                                              # pictures uploaded from the TOC helper
    out = write(by_key)
    ni = fill_images(out, retry=a.retry_images)                      # images only for the selected papers
    fill_from_image_search(out)                                      # title search for papers still without a picture
    cleanup_toc(by_key)
    write_need(out)
    write(by_key)
    with_img = sum(1 for p in out if p.get("img"))
    print(f"added {added}, removed {pruned} off-topic + {left_out} below the monthly top {PER_MONTH}, DOIs found {nd}, "
          f"abstracts filled {na}, images filled {ni}; "
          f"total {len(out)} papers, {with_img} with an image → {OUT}")
    if out_of_time():
        print(f"Time budget reached before the end. Run the workflow again with the same start date ({since}) "
              "to continue; papers already saved are skipped.")


if __name__ == "__main__":
    main()
