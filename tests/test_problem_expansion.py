import json
from pathlib import Path

import pytest

from scripts.problem_expansion import code_records, constant_weight_records, finite_rating, clay_records

ROOT = Path(__file__).resolve().parents[1]


def test_code_table_excludes_exact_cells_and_missing_constructions():
    html = '''<td><a href="BKLC.php?q=2&amp;n=65&amp;k=9">28-29</a></td>
    <td><a href="BKLC.php?q=2&n=64&k=9">28</a></td>
    <td BGCOLOR=FFA0A0><a href="BKLC.php?q=2&n=66&k=9">28-30</a></td>'''
    rows = code_records(html, 2)
    assert [r['parameters'] for r in rows] == [{'q': 2, 'n': 65, 'k': 9}]
    assert rows[0]['bounds']['lower'] == 28
    assert rows[0]['source_url'].endswith('q=2&n=65&k=9')


def test_constant_weight_table_uses_coordinates_not_reference_numbers():
    html = '''<a name="d4"></a><table><tr><th>n\\w</th><th>4</th></tr>
      <tr><th>10</th><td>5-10</td></tr></table>
      <a name="d6"></a><table><tr><th>n\\w</th><th>5</th><th>6</th><th>7</th></tr>
      <tr><th>16</th><td>48</td><td>109<sup>12</sup>-122</td><td><span class="lost">120-138</span></td></tr></table>'''
    rows = constant_weight_records(html)
    assert len(rows) == 1
    assert rows[0]['parameters'] == {'n': 16, 'd': 6, 'w': 6}
    assert rows[0]['bounds']['lower'] == 109
    assert rows[0]['source_url'].endswith('#d6')


def test_ratings_distinguish_verification_size_from_bound_gap():
    bounds = {'lower': 28, 'upper': 29}
    assert finite_rating('linear-code', {'q': 2, 'k': 9}, bounds)['score'] == 5
    assert finite_rating('linear-code', {'q': 2, 'k': 30}, bounds)['score'] == 3
    assert finite_rating('linear-code', {'q': 2, 'k': 9}, {'lower': 28, 'upper': 31})['score'] == 4
    assert 'not the cost or likelihood' in finite_rating('difference-set', {'v': 243})['reason']


def test_removed_difference_set_collection_is_absent_from_catalogue():
    data = json.loads((ROOT / 'agent_monitor/data/open_problems.json').read_text())
    assert not data['compact_families']
    assert 'difference-sets' not in data['counts']
    assert all(row['source'] != 'difference-sets' for row in data['problems'])
    assert all(source['id'] != 'difference-sets' for source in data['sources'])


def test_parameter_collections_are_not_in_the_selected_catalogue():
    data = json.loads((ROOT / 'agent_monitor/data/open_problems.json').read_text())
    removed = {'covering-designs', 'linear-codes', 'constant-weight-codes', 'ramsey-numbers'}
    assert removed.isdisjoint(data['counts'])
    assert all(row['source'] not in removed for row in data['problems'])
    assert all(source['id'] not in removed for source in data['sources'])


def test_clay_list_does_not_include_solved_poincare_problem():
    rows = clay_records()
    assert len(rows) == 6
    assert all('poincar' not in r['id'] for r in rows)


def test_malformed_code_table_fails_closed():
    with pytest.raises(ValueError):
        code_records('<td><a href="BKLC.php?q=3&n=65&k=9">28-29</a></td>', 2)
