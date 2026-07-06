from __future__ import annotations

import pytest

from oolu.skills.pack import (
    load_skill_pack,
    load_starter_pack,
    parse_skill_pack,
    starter_pack_text,
)
from oolu.skills.registry import SkillRegistry

_PACK = {
    "skills": [
        {
            "skill_id": "web.dropdown",
            "name": "Dynamic Dropdown",
            "summary": "select from a dynamic dropdown",
            "semver": "1.2.0",
            "tags": ["ui", "dropdown"],
            "adapter": "browser",
            "parameters": [{"name": "selector", "value_type": "string"}],
            "actions": [{"operation": "click"}, {"operation": "select_option"}],
        }
    ]
}


@pytest.fixture
def registry(tmp_path):
    reg = SkillRegistry(tmp_path / "reg.db")
    yield reg
    reg.close()


def test_load_pack_registers_skills(registry):
    (entry,) = load_skill_pack(registry, _PACK)
    assert entry.skill_id == "web.dropdown"
    assert entry.semver == "1.2.0"
    assert entry.tags == ["ui", "dropdown"]
    assert [p.name for p in entry.skill.parameters] == ["selector"]
    assert [a.operation for a in entry.skill.actions] == ["click", "select_option"]
    assert all(a.adapter == "browser" for a in entry.skill.actions)


def test_loading_the_same_pack_twice_is_idempotent(registry):
    first = load_skill_pack(registry, _PACK)
    second = load_skill_pack(registry, _PACK)
    assert first[0].content_hash == second[0].content_hash
    assert len(registry.list()) == 1


def test_skill_id_is_derived_from_name_when_absent(registry):
    pack = {
        "skills": [
            {
                "name": "Extract a Paginated Table",
                "summary": "read all rows",
                "actions": [{"operation": "read_rows"}],
            }
        ]
    }
    (entry,) = load_skill_pack(registry, pack)
    assert entry.skill_id == "extract.a.paginated.table"


def test_starter_pack_is_valid_and_searchable(registry):
    registered = load_starter_pack(registry)
    ids = {r.skill_id for r in registered}
    assert {"web.dynamic_dropdown", "web.paginated_table", "web.twofa_intercept"} <= ids

    hits = registry.search("extract table across pages", limit=3)
    assert hits[0].skill.skill_id == "web.paginated_table"


def test_starter_pack_parses_as_a_pack():
    import yaml

    pack = parse_skill_pack(yaml.safe_load(starter_pack_text()))
    assert len(pack.skills) >= 5
    assert all(entry.actions for entry in pack.skills)
