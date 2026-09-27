"""Build the selected research catalogue from pinned metadata and reviewed details.

This module reads local JSON; it never downloads or executes source material.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CURATION = ROOT / 'agent_monitor/data/open_problem_curation.json'
KEEP = frozenset({'erdos','kourovka','clay'})
NEW = frozenset({'ramanujan','hilbert','smale'})
CLAY_SOURCE = {
    'id': 'clay', 'name': 'Millennium Prize Problems', 'abbreviation': 'CM',
    'url': 'https://www.claymath.org/millennium-problems/',
    'attribution': 'Clay Mathematics Institute and the authors of the official problem descriptions',
    'license': 'Bibliographic pointers and original short summaries',
    'revision': '2026-09-10',
}


def _source_links(links):
    if not isinstance(links,list) or not links:
        raise ValueError('Each curated mathematical statement/result needs a source')
    for source in links:
        if not isinstance(source, dict) or not isinstance(source.get('url'), str):
            raise ValueError('Curated source links must be labelled HTTPS URLs without credentials')
        parts=urlsplit(source.get('url',''))
        if not source.get('label') or parts.scheme!='https' or not parts.hostname or parts.username or parts.password:
            raise ValueError('Curated source links must be labelled HTTPS URLs without credentials')


def _required_text(row, fields, context):
    for field in fields:
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f'{context} requires nonempty {field}')


def _validate_record_metadata(row):
    _required_text(row, ('id', 'number', 'title', 'summary', 'source', 'source_name',
                        'source_url', 'source_locator', 'status', 'status_as_of',
                        'source_status', 'source_revision'), 'Problem metadata')
    _source_links([{'label': row['source_name'], 'url': row['source_url']}])
    if not isinstance(row.get('topics'), list) or any(
            not isinstance(topic, str) or not topic.strip() for topic in row['topics']):
        raise ValueError('Problem metadata requires a list of topic labels')
    if row.get('additional_sources'):
        _source_links(row['additional_sources'])


def validate_details(row):
    statement=row.get('statement')
    if statement:
        if statement.get('kind') not in {'editorial','exact'} or not statement.get('text','').strip():
            raise ValueError('Invalid curated statement')
        _source_links(statement.get('sources'))
    for field in ('partial_results','proof_claims'):
        for item in row.get(field,[]):
            if not all(item.get(k) for k in ('title','text','status')):
                raise ValueError('Results require title, text and explicit status')
            _source_links(item.get('sources'))
    if row.get('proof_claims') and not statement:
        raise ValueError('A proof claim needs a documented problem statement')


def curate(base: dict, curation_path: Path = CURATION) -> dict:
    try:
        from scripts.problem_expansion import annotate, clay_records, RATING_METHOD
    except ModuleNotFoundError:
        from problem_expansion import annotate, clay_records, RATING_METHOD
    content=curation_path.read_bytes()
    curation=json.loads(content)
    if curation.get('schema_version')!=1:
        raise ValueError('Unsupported editorial curation schema')
    records=[deepcopy(row) for row in base['problems'] if row['source'] in KEEP]
    if not any(row['source']=='clay' for row in records):
        records.extend(clay_records())
    by_id={row['id']:row for row in records}
    if len(by_id)!=len(records):
        raise ValueError('Duplicate base problem ID')
    for key,details in curation.get('overlays',{}).items():
        if key not in by_id:
            raise ValueError('Curated overlay has no retained source problem: '+key)
        if any(field in details for field in ('id','number','source','source_url',
                                             'source_name','source_locator')):
            raise ValueError('Overlay cannot replace canonical source identity')
        by_id[key].update(deepcopy(details))
    for row in curation.get('problems',[]):
        if row.get('source') not in NEW or row.get('id') in by_id:
            raise ValueError('Invalid new collection or duplicate problem ID')
        row=deepcopy(row)
        records.append(row);by_id[row['id']]=row
    for row in records:
        if row['source'] == 'clay':
            # The original standalone Clay adapter predates source_name. Its
            # rows must match the metadata produced by the expansion importer.
            row.setdefault('source_name', CLAY_SOURCE['name'])
        _validate_record_metadata(row)
        annotate(row)
        row.setdefault('formal_status', 'No proof certified by this catalogue')
        if row.get('statement') and row['source'] != 'clay':
            row['rating']['reason'] = 'Read the statement and cited progress, then choose a tractable special case. A curated starting point does not predict a full solution.'
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,159}',row['id']):
            raise ValueError('Invalid canonical problem ID')
        if row.get('status') not in {'source-open','resolution-announced'}:
            raise ValueError('Unsupported catalogue status')
        if row['source'] in NEW and not row.get('statement'):
            raise ValueError('New highlights must have a curated statement')
        validate_details(row)
    if len({(r['source'],r['number']) for r in records})!=len(records):
        raise ValueError('Duplicate source locator')
    counts=dict(Counter(row['source'] for row in records))
    sources=[deepcopy(item) for item in base.get('sources',[]) if item['id'] in KEEP|{'conjecturebench'}]
    if not any(item['id']=='clay' for item in sources):
        sources.append(deepcopy(CLAY_SOURCE))
    sources.extend(deepcopy(curation.get('sources',[])))
    source_ids = [source.get('id') for source in sources]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError('Duplicate collection provenance ID')
    if not (KEEP | NEW).issubset(source_ids):
        raise ValueError('Collection provenance missing')
    for source in sources:
        _required_text(source, ('id', 'name', 'url', 'attribution', 'license', 'revision'),
                       'Collection provenance')
        _source_links([{'label': source['name'], 'url': source['url']}])
    order={'erdos':0,'kourovka':1,'clay':2,'ramanujan':3,'hilbert':4,'smale':5,'conjecturebench':6}
    sources.sort(key=lambda row:order.get(row['id'],99))
    # Put researched highlights first within each collection without losing
    # the source IDs used by existing links and forum threads.
    records.sort(key=lambda row:(order[row['source']],not bool(row.get('statement'))))
    return {
        'schema_version':2,'snapshot_date':curation['as_of'],'total':len(records),'counts':counts,
        'status_note':'Source-listed research problems at the dates shown. Announced resolutions are labelled separately; source listings and reported proofs are not independent verification by Ansätze.',
        'content_note':'Selected highlights include original editorial statements and sourced partial results or proof claims. Other entries remain bibliographic pointers to their source statements; their literature is not claimed to be exhaustively reviewed.',
        'sources':sources,'problems':records,'compact_families':[],
        'rating_method':deepcopy(base.get('rating_method') or RATING_METHOD),
        'source_input_hashes':{'editorial_curation':hashlib.sha256(content).hexdigest()},
        'kind_counts':dict(Counter(row['kind'] for row in records)),
        'status_counts':dict(Counter(row['status'] for row in records)),
        'detail_count':sum(bool(row.get('statement')) for row in records),
    }


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'agent_monitor/data/open_problems.json')
    parser.add_argument('--curation',type=Path,default=CURATION)
    parser.add_argument('--output',type=Path,default=ROOT/'agent_monitor/data/open_problems.json')
    args=parser.parse_args()
    data=curate(json.loads(args.input.read_text()),args.curation)
    text=json.dumps(data,ensure_ascii=False,separators=(',',':'))+'\n'
    for path in (args.output,ROOT/'docs/public/open-problems.json',ROOT/'docs/public/research/open-problems.json'):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    print(json.dumps({key:data[key] for key in ('total','counts','status_counts','detail_count')}))


if __name__=='__main__':main()
