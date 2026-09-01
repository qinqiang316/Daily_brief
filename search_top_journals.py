import urllib.request
import urllib.parse
import json
import time

def search_works(query=None, host_venue_issn=None, filter_str="", per_page=20):
    base_url = "https://api.openalex.org/works"
    filters = ["from_publication_date:2023-09-01", "to_publication_date:2026-09-01", "type:article"]
    if filter_str:
        filters.append(filter_str)
    
    params = {
        "filter": ",".join(filters),
        "per_page": per_page,
        "sort": "publication_date:desc"
    }
    if query:
        params["search"] = query
        
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

# Target specific targeted queries
targeted_queries = [
    # Maglev & Eddy Current Brake
    ("maglev eddy current brake", ""),
    ("electromagnetic brake high speed train", ""),
    ("eddy current brake rail vehicle", ""),
    ("maglev braking deceleration landing gear", ""),
    ("high-speed maglev emergency braking", ""),
    
    # Brake Blending & Control
    ("train electric pneumatic brake blending control", ""),
    ("train electro-mechanical brake clamping force", ""),
    ("train anti-skid braking adhesion control", ""),
    ("train braking trajectory optimization MPC", ""),
    
    # Friction, Wear, Thermal Crack
    ("train brake disc thermal cracking fatigue", ""),
    ("railway brake pad friction wear high speed", ""),
    ("carbon ceramic brake disc railway", ""),
    ("brake disc temperature field thermal stress high-speed train", ""),
    
    # Regenerative & Mechanical Braking
    ("train regenerative braking energy optimization timetable", ""),
    ("hybrid energy storage regenerative braking train", ""),
    
    # Fault Diagnosis & Safety
    ("train brake system fault diagnosis safety", ""),
    ("electro-pneumatic brake valve leakage fault train", ""),
    ("train EBCU fault diagnosis", ""),
    
    # Chinese Core translations / specific journals
    ("brake Railway Engineering Science", ""),
    ("brake Journal of Modern Transportation", ""),
    ("brake Chinese Journal of Mechanical Engineering", ""),
    ("brake Journal of Central South University", ""),
    ("brake Journal of the China Railway Society", "")
]

top_results = {}

for q, extra in targeted_queries:
    print(f"Targeted search: {q}")
    data = search_works(query=q, filter_str=extra, per_page=15)
    for work in data.get('results', []):
        doi = work.get('doi')
        if not doi:
            continue
        doi_clean = doi.lower().replace("https://doi.org/", "").strip()
        title = work.get('title') or ""
        # Check relevance
        title_lower = title.lower()
        if not any(k in title_lower for k in ["brake", "braking", "friction", "wear", "eddy current", "disc", "pad", "maglev", "adhesion", "retarder", "deceleration"]):
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
        
        top_results[doi_clean] = {
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

print(f"Total targeted papers: {len(top_results)}")
with open("targeted_results.json", "w", encoding="utf-8") as f:
    json.dump(top_results, f, ensure_ascii=False, indent=2)

