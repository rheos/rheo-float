"""``docextract_adapter.docextract_version``: what a PDF's parse inputs and the input
fingerprint record of the installed ``doc-extraction-api``.

Seam: the function over fake installed distributions (``importlib.metadata`` replaced
for the test). A git install names its commit; a path or editable install has no
commit in its install record and gives the bare version; a missing distribution fails
loudly rather than hashing a guess.
"""

from __future__ import annotations

import importlib.metadata
import json

import pytest

from rheo_finance.parsers import docextract_adapter


class FakeDistribution:
    def __init__(self, version: str, record: dict[str, object] | None) -> None:
        self.version = version
        self._record = record

    def read_text(self, name: str) -> str | None:
        if name != "direct_url.json" or self._record is None:
            return None
        return json.dumps(self._record)


def installed(
    monkeypatch: pytest.MonkeyPatch, version: str, record: dict[str, object] | None
) -> None:
    def distribution(name: str) -> FakeDistribution:
        assert name == docextract_adapter.DISTRIBUTION
        return FakeDistribution(version, record)

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)


def test_a_git_install_names_its_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    installed(
        monkeypatch,
        "0.1.0",
        {
            "url": "https://example.test/doc-extraction-api",
            "vcs_info": {"vcs": "git", "commit_id": "9" * 40},
        },
    )
    assert docextract_adapter.docextract_version() == "0.1.0+" + "9" * 40


def test_a_new_commit_under_one_version_is_a_new_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def at(commit: str) -> str:
        installed(monkeypatch, "0.1.0", {"vcs_info": {"commit_id": commit}})
        return docextract_adapter.docextract_version()

    assert at("9" * 40) != at("9" * 39 + "8")


@pytest.mark.parametrize(
    "record",
    [
        {"url": "file:///tmp/doc-extraction-api", "dir_info": {"editable": True}},
        {"url": "file:///tmp/doc-extraction-api", "dir_info": {}},
        None,
    ],
)
def test_a_path_or_editable_install_gives_the_bare_version(
    monkeypatch: pytest.MonkeyPatch, record: dict[str, object] | None
) -> None:
    installed(monkeypatch, "0.2.0", record)
    assert docextract_adapter.docextract_version() == "0.2.0"


def test_a_missing_distribution_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    def distribution(name: str) -> FakeDistribution:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    with pytest.raises(importlib.metadata.PackageNotFoundError):
        docextract_adapter.docextract_version()


def test_the_real_install_is_found() -> None:
    assert docextract_adapter.docextract_version()
