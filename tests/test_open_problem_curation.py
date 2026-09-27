"""Contracts for reviewed statements, claim status and stable source identities."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.curate_open_problems import CURATION, curate, validate_details

ROOT = Path(__file__).resolve().parents[1]


def catalogue():
    return json.loads((ROOT / 'agent_monitor/data/open_problems.json').read_text())


def test_curating_current_metadata_is_reproducible_and_preserves_thread_ids():
    current = catalogue()
    assert curate(current) == current
    assert current['counts'] == {'erdos': 590, 'kourovka': 1151, 'clay': 6,
                                 'ramanujan': 3, 'hilbert': 2, 'smale': 4}
    rows = {r['id']: r for r in current['problems']}
    assert rows['erdos-0003']['source_url'] == 'https://www.erdosproblems.com/3'
    assert rows['kourovka-01-003']['source_url'].endswith('/21tkt.pdf#page=5')
    assert rows['kourovka-12-020']['source_url'].endswith('/21tkt.pdf#page=59')


def test_fresh_import_and_existing_catalogue_have_identical_clay_metadata():
    current = catalogue()
    fresh = deepcopy(current)
    fresh['problems'] = [r for r in fresh['problems'] if r['source'] != 'clay']
    fresh['sources'] = [s for s in fresh['sources'] if s['id'] != 'clay']
    assert curate(fresh) == current


def test_curated_detail_and_claim_provenance_is_complete():
    data = catalogue()
    detailed = [r for r in data['problems'] if r.get('statement')]
    assert len(detailed) == 18
    for row in detailed:
        validate_details(row)
        assert row['statement']['kind'] == 'editorial'
        assert row['details_as_of'] == '2026-09-16'
        assert row['partial_results']
        assert isinstance(row['proof_claims'], list)
    pointers = [r for r in data['problems'] if not r.get('statement')]
    assert pointers and all(r['source'] in {'erdos','kourovka'} for r in pointers)
    assert not any(r.get('proof_claims') for r in pointers)


def test_announcements_and_failed_claims_do_not_become_accepted_proofs():
    rows = {r['id']: r for r in catalogue()['problems']}
    navier = rows['clay-navier-stokes-equation']
    assert navier['status'] == 'resolution-announced'
    assert navier['proof_claims'][0]['status'] == 'announced'
    assert any('claymath.org/news/' in s['url'] for s in navier['proof_claims'][0]['sources'])
    assert 'nu>0' in navier['statement']['text']
    assert 'polynomial-length certificates' in rows['clay-p-vs-np']['statement']['text']
    assert rows['ramanujan-lehmer']['status'] == 'source-open'
    assert rows['ramanujan-lehmer']['proof_claims'][0]['status'] == 'Withdrawn proof claim'
    assert rows['hilbert-16-ii']['proof_claims'][0]['status'] == 'Refuted proof claim'
    assert 'open extension' in rows['hilbert-10-q']['title']
    assert 'complex' in rows['smale-11']['title']
    assert 'nonholomorphic' in rows['ramanujan-maass']['detail_note']


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'http://example.com', 'https://user:password@example.com', '/relative'])
def test_curated_source_urls_reject_unsafe_or_unattributed_links(url):
    with pytest.raises(ValueError, match='HTTPS'):
        validate_details({'statement': {'kind':'editorial', 'text':'A question', 'sources':[{'label':'Source', 'url':url}]}})


def test_claim_requires_a_statement_and_supporting_sources():
    claim = {'title':'Claim', 'text':'A reported claim', 'status':'unverified',
             'sources':[{'label':'Paper', 'url':'https://arxiv.org/abs/1506.02098'}]}
    with pytest.raises(ValueError, match='statement'):
        validate_details({'proof_claims':[claim]})
    with pytest.raises(ValueError, match='source'):
        validate_details({'partial_results':[{**claim,'sources':[]}]})


@pytest.mark.parametrize('field', ['id', 'number', 'source', 'source_url',
                                 'source_name', 'source_locator'])
def test_overlay_cannot_replace_canonical_source_identity(tmp_path, field):
    curation = json.loads(CURATION.read_text())
    curation['overlays']['erdos-0003'][field] = 'https://example.com'
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(curation))
    with pytest.raises(ValueError, match='canonical source identity'):
        curate(catalogue(), path)


@pytest.mark.parametrize('field', ['title', 'summary', 'source_name', 'source_url',
                                 'source_locator', 'source_revision', 'status_as_of'])
def test_new_problem_requires_metadata_used_by_catalogue_and_provenance(tmp_path, field):
    curation = json.loads(CURATION.read_text())
    del curation['problems'][0][field]
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(curation))
    with pytest.raises(ValueError, match='Problem metadata requires nonempty ' + field):
        curate(catalogue(), path)


def test_new_problem_source_url_is_validated_like_statement_citations(tmp_path):
    curation = json.loads(CURATION.read_text())
    curation['problems'][0]['source_url'] = 'javascript:alert(1)'
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(curation))
    with pytest.raises(ValueError, match='HTTPS'):
        curate(catalogue(), path)


def test_duplicate_collection_provenance_is_rejected(tmp_path):
    curation = json.loads(CURATION.read_text())
    curation['sources'].append(deepcopy(curation['sources'][0]))
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(curation))
    with pytest.raises(ValueError, match='Duplicate collection provenance'):
        curate(catalogue(), path)


def test_retained_collection_requires_its_original_provenance():
    base = catalogue()
    base['sources'] = [s for s in base['sources'] if s['id'] != 'erdos']
    with pytest.raises(ValueError, match='Collection provenance missing'):
        curate(base)
