import urllib.request
import urllib.parse
import json
import time

def search_openalex(query, filter_extra="", per_page=15):
    base_url = "https://api.openalex.org/works"
    filters = "from_publication_date:2023-09-01,to_publication_date:2026-09-01,type:article"
    if filter_extra:
        filters += f",{filter_extra}"
    params = {
        "search": query,
        "filter": filters,
        "per_page": per_page,
        "sort": "cited_by_count:desc"
    }
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={'User-Agent': 'academic_brief_bot/1.0 (mailto:academic@research.org)'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Error querying {query}: {e}")
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

queries = [
    # 1. 磁浮制动与涡流制动
    "maglev eddy current brake",
    "maglev train braking system",
    "linear eddy current brake high-speed train",
    "electromagnetic brake rail vehicle",
    # 2. 制动控制、制动融合与线控制动
    "train braking control blending",
    "electro-pneumatic brake blending high-speed train",
    "train brake-by-wire electro-mechanical braking",
    "railway braking adhesion control",
    # 3. 制动盘/闸片摩擦磨损、热安全与热疲劳
    "high-speed train brake disc thermal fatigue",
    "railway brake disc pad friction wear thermal crack",
    "carbon ceramic brake disc high-speed train",
    # 4. 再生制动与机械制动协同
    "train regenerative braking mechanical braking coordination",
    "urban rail regenerative braking energy management storage",
    # 5. 制动系统故障诊断与安全
    "train brake system fault diagnosis",
    "railway electro-pneumatic brake valve fault diagnosis",
    # 6. 中文核心期刊关键词（拼音/英文翻译）
    "China Railway Science brake system",
    "Journal of the China Railway Society brake",
    "Journal of Traffic and Transportation Engineering brake"
]

all_results = {}

for q in queries:
    print(f"Searching: {q}")
    data = search_openalex(q, per_page=10)
    for work in data.get('results', []):
        doi = work.get('doi')
        if not doi:
            continue
        doi_clean = doi.lower().replace("https://doi.org/", "").strip()
        if doi_clean not in all_results:
            title = work.get('title')
            pub_date = work.get('publication_date')
            source = work.get('primary_location', {}).get('source', {})
            journal_name = source.get('display_name') if source else None
            host_org = source.get('host_organization_name') if source else None
            is_oa = work.get('open_access', {}).get('is_oa')
            oa_url = work.get('open_access', {}).get('oa_url')
            authors = [a.get('author', {}).get('display_name') for a in work.get('authorships', [])]
            abstract = reconstruct_abstract(work.get('abstract_inverted_index'))
            cited_by = work.get('cited_by_count', 0)
            
            all_results[doi_clean] = {
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

print(f"Total unique papers found: {len(all_results)}")
with open("openalex_results.json", "w", encoding="utf-8") as f:
    json.dump(all_results, f, ensure_ascii=False, indent=2)

