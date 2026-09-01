import urllib.request
import urllib.parse
import json
import time

chinese_journals = [
    ("铁道学报", "Journal of the China Railway Society", "1001-8360"),
    ("中国铁道科学", "China Railway Science", "1001-4632"),
    ("交通运输工程学报", "Journal of Traffic and Transportation Engineering", "1671-1637"),
    ("机械工程学报", "Journal of Mechanical Engineering", "0577-6686"),
    ("铁道科学与工程学报", "Journal of Railway Science and Engineering", "1672-7029"),
    ("摩擦学学报", "Tribology", "1004-0595"),
    ("西南交通大学学报", "Journal of Southwest Jiaotong University", "0258-2724"),
    ("中南大学学报(自然科学版)", "Journal of Central South University", "2095-2899")
]

results = []

for cname, ename, issn in chinese_journals:
    print(f"Searching for {cname} / {ename}...")
    # Search Crossref
    for q in ["制动", "brake", "friction", "wear"]:
        url = f"https://api.crossref.org/works?query={urllib.parse.quote(q)}&query.container-title={urllib.parse.quote(ename)}&filter=from-pub-date:2023-09-01,until-pub-date:2026-09-01,type:journal-article&rows=10"
        req = urllib.request.Request(url, headers={'User-Agent': 'academic_brief_bot/1.0 (mailto:academic@research.org)'})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                for it in data.get('message', {}).get('items', []):
                    title = it.get('title', [''])[0]
                    doi = it.get('DOI', '')
                    jname = it.get('container-title', [''])[0]
                    issued = it.get('issued', {}).get('date-parts', [[None]])[0]
                    pub_date = "-".join([str(x).zfill(2) for x in issued if x is not None])
                    authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get('author', [])]
                    
                    title_lower = title.lower()
                    if any(k in title_lower for k in ["brake", "braking", "friction", "wear", "disc", "pad", "eddy current", "retarder", "maglev", "adhesion", "anti-skid", "regenerative", "制动", "摩擦", "磨损"]):
                        results.append({
                            "doi": doi,
                            "title": title,
                            "journal": jname or cname,
                            "chinese_journal": cname,
                            "pub_date": pub_date,
                            "authors": authors,
                            "abstract": it.get('abstract', ''),
                            "source": "Crossref-ChineseCore"
                        })
        except Exception as e:
            pass
        time.sleep(0.2)

print(f"Chinese core journals search complete. Found {len(results)} candidate papers.")
with open("chinese_core_papers.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

