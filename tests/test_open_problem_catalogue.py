"""Provenance and fail-closed selection checks for the research catalogue."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("problem_importer", ROOT / "scripts/import_open_problems.py")
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


def test_public_and_server_snapshots_agree_and_have_real_unique_references():
    server = (ROOT / "agent_monitor/data/open_problems.json").read_bytes()
    assert server == (ROOT / "docs/public/open-problems.json").read_bytes()
    data = json.loads(server)
    assert server == (ROOT / "docs/public/research/open-problems.json").read_bytes()
    records = list(importer.all_records(data))
    assert data["total"] == len(records) == 1756
    assert len({row["id"] for row in records}) == len(records)
    assert len({(row["source"], row["number"]) for row in records}) == len(records)
    assert sum(data["counts"].values()) == len(records)
    for row in records:
        assert row["status"] in {"source-open", "resolution-announced"}
        assert row["status_as_of"]
        assert row["source_revision"]
        assert row["source_url"].startswith("https://")
        assert row["source_locator"] and row["topics"]
        assert row["rating"]["score"] in range(1, 6)
        assert row["rating"]["reason"] and row["rating"]["method"] == "exploration-fit-v1"
    assert data["kind_counts"] == {"research-question": 1756}
    assert data["detail_count"] == 18


def test_erdos_excludes_solved_and_ambiguous_statuses():
    rows = [{"number": str(i), "informal_status": {"state": status}, "tags": ["number theory"]}
            for i, status in enumerate(["open", "proved", "disproved", "solved", "independent", "falsifiable"], 1)]
    assert [row["number"] for row in importer.erdos_records(rows)] == ["1"]


def test_cross_collection_duplicate_is_one_problem_with_two_source_links():
    records = json.loads((ROOT / "agent_monitor/data/open_problems.json").read_text())["problems"]
    by_id = {row["id"]: row for row in records}
    assert "kourovka-21-093" not in by_id
    assert by_id["erdos-0274"]["additional_sources"][0]["relation"] == "same-problem"
    assert "erdos-1163" not in by_id
    assert by_id["erdos-1162"]["additional_sources"][0]["relation"] == "related-ambiguous-formulation"


def test_notebook_excludes_answered_parts_and_solved_archive(tmp_path):
    for number in range(1, 6):
        (tmp_path / f"{number}.json").write_text(json.dumps({
            "id": f"kourovka-main-01-{number:03d}",
            "status_observation": {"state": "listed-unsolved"},
        }))
    pages = ["1.1. A question about finite groups.\n∗1.2. An answered question.\n∗ Yes.\n",
             "1.3. A question with answered parts.\n∗ a) Answered.\n1.4. Another question about free groups.\n",
             "Archive of solved problems\n1.5. This is solved.\n"]
    records = importer.notebook_records(tmp_path, pages)
    assert [row["number"] for row in records] == ["1.1", "1.4"]
    assert records[0]["source_url"].endswith("#page=1")
    assert records[1]["source_url"].endswith("#page=2")
    assert "free groups" in records[1]["topics"]


def test_notebook_requires_identifiable_archive_boundary(tmp_path):
    with pytest.raises(ValueError, match="solved archive"):
        importer.notebook_records(tmp_path, ["1.1. A question.\n"])


def test_known_newly_answered_notebook_questions_are_excluded():
    records = json.loads((ROOT / "agent_monitor/data/open_problems.json").read_text())["problems"]
    ids = {row["id"] for row in records}
    # The editors' September update explicitly answers these July-open entries.
    assert ids.isdisjoint({"kourovka-06-030", "kourovka-10-034", "kourovka-11-009",
                          "kourovka-12-015", "kourovka-14-085", "kourovka-21-150"})
    # The current official feed lists these as disproved/proved, respectively.
    assert "erdos-0001" not in ids and "erdos-0004" not in ids
