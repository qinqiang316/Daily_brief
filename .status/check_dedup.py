import json
import re

with open("/Users/qqiang/AI project/05-日常工具/DailyBrief/.status/history_fingerprints.json", "r", encoding="utf-8") as f:
    history = json.load(f)

def normalize_title(title):
    return re.sub(r'[^\w\u4e00-\u9fff]+', '', title).lower()

def normalize_doi(doi):
    doi = doi.lower().strip()
    doi = re.sub(r'^https?://(dx\.)?doi\.org/', '', doi)
    return doi

def normalize_url(url):
    url = url.strip()
    url = re.sub(r'\?.*$', '', url)
    url = re.sub(r'arxiv\.org/(html|pdf)/([0-9]+\.[0-9]+)(\.pdf)?', r'arxiv.org/abs/\2', url)
    return url

candidates = [
    # Rail
    {"domain": "rail", "title": "Fixed-Time Prescribed Performance Terminal Sliding Mode Control for Maglev Levitation System with Input Saturation and External Disturbances", "doi": "10.1109/TTE.2026.3720610", "url": "https://doi.org/10.1109/TTE.2026.3720610"},
    {"domain": "rail", "title": "Enhancement of the stability of maglev sled based on the secondary suspension system", "doi": "10.1080/00423114.2026.2685178", "url": "https://doi.org/10.1080/00423114.2026.2685178"},
    {"domain": "rail", "title": "Koopman Neural Operator-based surrogate modelling for high-frequency dynamics of high-speed rail pantographs", "doi": "10.1016/j.ymssp.2026.114780", "url": "https://doi.org/10.1016/j.ymssp.2026.114780"},
    
    # Braking
    {"domain": "braking", "title": "Characteristic Analysis of Eddy Current Braking System with AC Excitation and Auxiliary Capacitor", "doi": "10.3390/en19092118", "url": "https://doi.org/10.3390/en19092118"},
    {"domain": "braking", "title": "面向高速动车组用碳陶复合材料制动盘及地面试验研究", "doi": "10.3969/j.issn.1000-128X.2026.03.001", "url": "https://kns.cnki.net/kcms2/article/abstract?v=railway_rolling_stock_2026_03"},
    {"domain": "braking", "title": "地铁列车电子机械制动夹钳系统研制", "doi": "10.3969/j.issn.1002-7602.2026.02.005", "url": "https://kns.cnki.net/kcms2/article/abstract?v=rolling_stock_2026_02_emb"},
    
    # Cross domain
    {"domain": "cross", "title": "Covariance-Regulated Recursive Koopman Learning for Nonlinear Systems with Uncertain Time-Varying Dynamics", "doi": "", "url": "https://arxiv.org/abs/2606.15317"},
    {"domain": "cross", "title": "Data-Driven Domain of Attraction Estimation: Zubov--Koopman Operator on an RKHS and Its Spectrum", "doi": "", "url": "https://arxiv.org/abs/2608.01018"}
]

seen_titles = set(history["titles"])
seen_dois = set(history["dois"])
seen_urls = set(history["urls"])

print("--- Deduplication Check ---")
for c in candidates:
    n_title = normalize_title(c["title"])
    n_doi = normalize_doi(c["doi"]) if c["doi"] else ""
    n_url = normalize_url(c["url"])
    
    dup = False
    dup_reason = []
    if n_title in seen_titles:
        dup = True
        dup_reason.append("Title matched history")
    if n_doi and n_doi in seen_dois:
        dup = True
        dup_reason.append("DOI matched history")
    if n_url in seen_urls:
        dup = True
        dup_reason.append("URL matched history")
        
    print(f"[{c['domain']}] {c['title'][:50]}... -> Dup: {dup} ({', '.join(dup_reason) if dup else 'PASS'})")
