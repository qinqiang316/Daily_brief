"""Bounded script discovery and immutable page snapshots for agent lanes.

Search snippets are discovery only. Authors/reviewers use fetched page text and
metadata, with the exact extraction/hash contract shared by the supervisor.
"""
import hashlib
import re
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from modules import filter as fm, rank, retrieve, run_store
from modules.region import bind_lane


def collect(task, budget):
    raw, sources, stats = list(task.get('initial_items') or []), {}, {}
    queries = []
    for query in task['queries']:
        q = dict(query)
        q['query'] += ' after:%s before:%s' % (
            task['window']['start'],
            (datetime.strptime(task['window']['end'], '%Y-%m-%d')
             + timedelta(days=1)).strftime('%Y-%m-%d'))
        q['max_results'] = min(3, q.get('max_results', 3))
        queries.append(q)
    search_budget = budget.derive(min(120, budget.remaining()/2))
    def search_group(index, group):
        found, mapping, counts = [], {}, {}
        rank.search_batch_with_tags(task['lane']+':'+str(index), group,
            found, counts, task.get('preference', {}).get('pref_dir'),
            src_map=mapping, budget=search_budget)
        return found, mapping, counts
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(search_group, i, group)
                   for i, group in enumerate(retrieve.chunked(queries, 5))]
        for future in futures:
            found, mapping, counts = future.result()
            raw.extend(found); sources.update(mapping); stats.update(counts)
    raw.sort(key=lambda item: (bool(item.get('is_explore')),
             item.get('direction') in ('轨道交通','健康','生活'),
             bool(item.get('is_preferred'))), reverse=True)
    pre = rank._cheap_precheck(raw, fm.load_dedup())[:30]
    pages = retrieve.fetch_texts_parallel([x['c']['url'] for x in pre],
        timeout=12, budget=budget, with_metadata=True)
    items, snapshots = [], {}
    for row in pre:
        page = pages.get(row['c']['url'])
        if not isinstance(page, dict):
            continue
        item = dict(row['c'], url=row['url'], content=page['text'],
                    metadata=page.get('metadata') or {})
        date = fm.extract_date_evidence(item)
        if (retrieve.wc(item['content']) < fm.MIN_WORDS
                or date['precision'] != 'day'
                or not task['window']['start'] <= (date['publish_date'] or '') <= task['window']['end']):
            continue
        item.update(publish_date=date['publish_date'], date_source=date['date_source'],
                    date_evidence=date['evidence'], date_precision='day')
        bind_lane(item, task['lane'])
        digest = hashlib.sha256(re.sub(r'\s+', '', page['text']).encode()).hexdigest()
        item['source_verification'] = {'content_sha256': digest, 'date': date}
        items.append(item)
        snapshots[item['url']] = page
    directory = Path(task['lane_dir'])
    path = directory / 'seed_sources.json'
    run_store.write_once(path, run_store.json_bytes(snapshots))
    result = directory / 'seed_items.json'
    run_store.write_once(result, run_store.json_bytes(dict(
        run_id=task['run_id'], lane=task['lane'], items=items, stats=stats)))
    return str(result), str(path), hashlib.sha256(path.read_bytes()).hexdigest()
