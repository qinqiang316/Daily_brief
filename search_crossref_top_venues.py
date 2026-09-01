import urllib.request
import urllib.parse
import json
import time

venues = [
    ("Wear", "brake OR braking OR disc OR pad"),
    ("Tribology International", "brake OR braking OR train"),
    ("Vehicle System Dynamics", "brake OR braking OR train"),
    ("Mechanical Systems and Signal Processing", "brake OR braking OR train"),
    ("IEEE Transactions on Intelligent Transportation Systems", "brake OR braking OR train"),
    ("IEEE Transactions on Transportation Electrification", "brake OR braking OR train"),
    ("Railway Engineering Science", "brake OR braking"),
    ("Chinese Journal of Mechanical Engineering", "brake OR braking"),
    ("Engineering Failure Analysis", "brake disc OR train brake"),
    ("Control Engineering Practice", "train brake OR braking")
]

results = []

for container, q in venues:
    print(f"Searching Crossref for {container} with query '{q}'...")
    url = f"https://api.crossref.org/works?query={urllib.parse.quote(q)}&query.container-title={urllib.parse.quote(container)}&filter=from-pub-date:2023-09-01,until-pub-date:2026-09-01,type:journal-article&rows=10"
    req = urllib.request.Request(url, headers={'User-Agent': 'academic_brief_bot/1.0 (mailto:academic@research.org)'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            items = data.get('message', {}).get('items', [])
            for it in items:
                title_list = it.get('title', [])
                if not title_list:
                    continue
                title = title_list[0]
                doi = it.get('DOI', '')
                jname = it.get('container-title', [''])[0]
                issued = it.get('issued', {}).get('date-parts', [[None]])[0]
                pub_date = "-".join([str(x).zfill(2) for x in issued if x is not None])
                authors = []
                for a in it.get('author', []):
                    given = a.get('given', '')
                    family = a.get('family', '')
                    authors.append(f"{given} {family}".strip())
                abstract = it.get('abstract', '')
                
                # Check relevance
                title_lower = title.lower()
                if any(k in title_lower for k in ["brake", "braking", "friction", "wear", "disc", "pad", "eddy current", "retarder", "maglev", "adhesion", "anti-skid", "regenerative"]):
                    results.append({
                        "doi": doi,
                        "title": title,
                        "journal": jname,
                        "pub_date": pub_date,
                        "authors": authors,
                        "abstract": abstract,
                        "source": "Crossref"
                    })
    except Exception as e:
        print(f"Error searching {container}: {e}")
    time.sleep(0.3)

print(f"Crossref top venues search complete. Found {len(results)} relevant papers.")
with open("crossref_top_papers.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

