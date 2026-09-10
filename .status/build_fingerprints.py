import os
import re
import json
import glob

def normalize_title(title):
    t = re.sub(r'[^\w\u4e00-\u9fff]+', '', title).lower()
    return t

def normalize_doi(doi):
    doi = doi.lower().strip()
    doi = re.sub(r'^https?://(dx\.)?doi\.org/', '', doi)
    return doi

def normalize_url(url):
    url = url.strip()
    url = re.sub(r'\?.*$', '', url)
    # arxiv html/pdf to abs
    url = re.sub(r'arxiv\.org/(html|pdf)/([0-9]+\.[0-9]+)(\.pdf)?', r'arxiv.org/abs/\2', url)
    return url

output_dir = "/Users/qqiang/AI project/05-日常工具/DailyBrief/output"
files = glob.glob(os.path.join(output_dir, "Monthly-Academic-Brief-*.md"))

history_data = {
    "dois": set(),
    "titles": set(),
    "urls": set(),
    "records": []
}

for fpath in files:
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Extract markdown links [Title](URL)
    links = re.findall(r'\[([^\]]+)\]\((https?://[^\)]+)\)', content)
    for title, url in links:
        n_title = normalize_title(title)
        n_url = normalize_url(url)
        history_data["titles"].add(n_title)
        history_data["urls"].add(n_url)
        
        # Extract DOI if present in URL
        doi_match = re.search(r'10\.\d{4,9}/[-._;()/:A-Za-z0-9]+', url)
        if doi_match:
            n_doi = normalize_doi(doi_match.group(0))
            history_data["dois"].add(n_doi)
            
        history_data["records"].append({
            "file": os.path.basename(fpath),
            "title": title,
            "url": url,
            "norm_title": n_title,
            "norm_url": n_url
        })
    
    # Also search for explicit DOIs in text
    all_dois = re.findall(r'10\.\d{4,9}/[-._;()/:A-Za-z0-9]+', content)
    for d in all_dois:
        history_data["dois"].add(normalize_doi(d))

out_json = {
    "dois": list(history_data["dois"]),
    "titles": list(history_data["titles"]),
    "urls": list(history_data["urls"]),
    "records": history_data["records"]
}

status_file = "/Users/qqiang/AI project/05-日常工具/DailyBrief/.status/history_fingerprints.json"
with open(status_file, "w", encoding="utf-8") as f:
    json.dump(out_json, f, ensure_ascii=False, indent=2)

print(f"Loaded {len(files)} historical files.")
print(f"Total DOIs: {len(out_json['dois'])}, Titles: {len(out_json['titles'])}, URLs: {len(out_json['urls'])}, Records: {len(out_json['records'])}")
