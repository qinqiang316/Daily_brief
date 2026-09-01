import urllib.request
import urllib.parse
import json
import time

def search_openalex_query(q, filter_extra="", per_page=25):
    base_url = "https://api.openalex.org/works"
    filters = ["from_publication_date:2023-09-01", "to_publication_date:2026-09-01", "type:article"]
    if filter_extra:
        filters.append(filter_extra)
    params = {
        "search": q,
        "filter": ",".join(filters),
        "per_page": per_page,
        "sort": "cited_by_count:desc"
    }
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={'User-Agent': 'academic_brief_bot/1.0 (mailto:academic@research.org)'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Error querying {q}: {e}")
        return {}

def reconstruct_abstract(abstract_inverted_index):
    if not abstract_inverted_index:
        return ""
    word_positions = []
    for word, positions in abstract_inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort(key=lambda x: x[0])
    return " ".join([w for p, w in word_positions])

# Search queries
search_list = [
    # 磁浮制动与涡流制动
    "maglev braking",
    "maglev eddy current brake",
    "high-speed maglev brake",
    "linear eddy current brake train",
    "eddy current retarder railway",
    "permanent magnet eddy current brake train",
    "electromagnetic brake train dynamics",
    
    # 制动控制、制动融合、防滑与线控制动
    "train braking control",
    "train electric pneumatic blending braking",
    "train electro-mechanical brake",
    "train anti-skid braking",
    "train braking adhesion optimization",
    "train braking deceleration control",
    
    # 摩擦磨损、制动盘/闸片热安全、碳陶制动盘
    "high-speed train brake disc thermal",
    "train brake disc thermal cracking",
    "train brake pad friction wear",
    "carbon ceramic brake disc train",
    "brake disc thermomechanical coupling train",
    "C/C-SiC brake railway",
    
    # 再生制动与能量协同
    "train regenerative braking energy optimization",
    "rail regenerative braking energy storage",
    "train regenerative mechanical braking coordination",
    
    # 故障诊断、可靠性与安全
    "train brake system fault diagnosis",
    "train electro-pneumatic brake fault",
    "train braking system safety evaluation",
    "train EBCU fault diagnosis",
    
    # 顶级期刊直接搜索
    "Wear train brake disc",
    "Tribology International train brake",
    "Vehicle System Dynamics train braking",
    "Mechanical Systems and Signal Processing train brake",
    "IEEE Transactions on Transportation Electrification train braking",
    "IEEE Transactions on Intelligent Transportation Systems train braking",
    "Railway Engineering Science braking"
]

all_papers = {}

# Load existing
try:
    with open("targeted_results.json", "r", encoding="utf-8") as f:
        all_papers.update(json.load(f))
except:
    pass

try:
    with open("openalex_results.json", "r", encoding="utf-8") as f:
        all_papers.update(json.load(f))
except:
    pass

print(f"Starting with {len(all_papers)} papers...")

for idx, q in enumerate(search_list):
    print(f"[{idx+1}/{len(search_list)}] Searching: {q}")
    data = search_openalex_query(q, per_page=20)
    for work in data.get('results', []):
        doi = work.get('doi')
        if not doi:
            continue
        doi_clean = doi.lower().replace("https://doi.org/", "").strip()
        title = work.get('title') or ""
        title_lower = title.lower()
        
        # Keyword relevance check
        keywords = ["brake", "braking", "friction", "wear", "eddy current", "disc", "pad", "maglev", "adhesion", "retarder", "deceleration", "regenerative"]
        if not any(k in title_lower for k in keywords):
            continue
            
        pub_date = work.get('publication_date')
        source = work.get('primary_location', {}).get('source', {})
        journal_name = source.get('display_name') if source else None
        host_org = source.get('host_organization_name') if source else None
        is_oa = work.get('open_access', {}).get('is_oa')
        oa_url = work.get('open_access', {}).get('oa_url')
        authors = [a.get('author', {}).get('display_name') for a in work.get('authorships', [])]
        abstract = reconstruct_abstract(work.get('abstract_inverted_index'))
        cited_by = work.get('cited_by_count', 0)
        
        all_papers[doi_clean] = {
            "doi": doi,
            "doi_clean": doi_clean,
            "title": title,
            "pub_date": pub_date,
            "journal": journal_name,
            "publisher": host_org,
            "authors": authors,
            "abstract": abstract,
            "cited_by": cited_by,
            "oa_url": oa_url,
            "query": q
        }
    time.sleep(0.3)

print(f"Total compiled papers: {len(all_papers)}")
with open("all_brake_papers.json", "w", encoding="utf-8") as f:
    json.dump(all_papers, f, ensure_ascii=False, indent=2)

