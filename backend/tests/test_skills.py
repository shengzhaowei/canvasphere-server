import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import skills  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skills"
ROOT_SKILLS = Path(__file__).resolve().parents[2] / "skills"


def test_load_fixture_skills():
    for name in ("storyboard", "image-prompt"):
        s = skills.load_skill(name, skills_dir=FIXTURES)
        assert s["name"] == name
        assert s["description"], f"{name} should have a description"
        assert s["content"], f"{name} should have a body/workflow"
    listed = skills.list_skills(skills_dir=FIXTURES)
    assert {s["name"] for s in listed} == {"storyboard", "image-prompt"}


def test_load_root_skills_or_skip():
    # skills/ is built by another person in parallel; if its SKILL.md
    # files are not there yet, skip honestly instead of faking a pass.
    for name in ("storyboard", "image-prompt"):
        path = ROOT_SKILLS / name / "SKILL.md"
        if not path.is_file():
            pytest.skip(f"root skills/{name}/SKILL.md not present yet (built in parallel)")
        s = skills.load_skill(name)
        assert s["name"] == name
        assert s["description"]
        assert s["content"]


def test_missing_skill_raises_clear_error():
    with pytest.raises(FileNotFoundError) as excinfo:
        skills.load_skill("does-not-exist", skills_dir=FIXTURES)
    assert "does-not-exist" in str(excinfo.value)
    with pytest.raises(FileNotFoundError):
        skills.load_skill("does-not-exist")
    with pytest.raises(ValueError):
        skills.load_skill("../escape", skills_dir=FIXTURES)


def test_skill_toggle_per_project():
    pid = "proj-toggle-test"
    assert skills.is_skill_enabled(pid, "storyboard") is True  # default on
    assert skills.set_skill_enabled(pid, "storyboard", False) is False
    assert skills.is_skill_enabled(pid, "storyboard") is False
    assert skills.is_skill_enabled("other-project", "storyboard") is True
    skills.set_skill_enabled(pid, "storyboard", True)
    assert skills.is_skill_enabled(pid, "storyboard") is True
