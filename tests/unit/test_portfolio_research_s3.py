from unittest.mock import MagicMock, patch

from services import portfolio_research_s3 as s3mod


def test_combo_object_key_from_manifest_file():
    artifact = {
        "storage": "s3",
        "s3": {
            "bucket": "aipoc",
            "prefix": "portfolio-research/j1/r1/",
            "combos_prefix": "portfolio-research/j1/r1/combos/",
        },
    }
    key = s3mod.combo_object_key(artifact, "combo-a", "job_combos/combo-a.json")
    assert key == "portfolio-research/j1/r1/combos/combo-a.json"


def test_fetch_combo_bytes(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_RESEARCH_S3_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_S3_BUCKET", "aipoc")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_S3_SECRET_KEY", "sk")
    artifact = {
        "storage": "s3",
        "s3": {
            "bucket": "aipoc",
            "combos_prefix": "portfolio-research/j1/r1/combos/",
        },
    }
    client = MagicMock()
    client.get_object.return_value = {"Body": MagicMock(read=lambda: b'{"ok":true}')}
    with patch.object(s3mod, "_s3_client", return_value=client):
        body = s3mod.fetch_combo_bytes(artifact, "combo-a", "combo-a.json")
    assert body == b'{"ok":true}'


def test_fetch_rejects_incomplete_s3_config(monkeypatch):
    for key in (
        "PORTFOLIO_RESEARCH_S3_ENDPOINT",
        "PORTFOLIO_RESEARCH_S3_BUCKET",
        "PORTFOLIO_RESEARCH_S3_ACCESS_KEY",
        "PORTFOLIO_RESEARCH_S3_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    artifact = {"storage": "s3", "s3": {"html_key": "portfolio-research/j1/r1/report.html"}}
    try:
        s3mod.fetch_report_html_bytes(artifact)
    except s3mod.ArtifactStorageUnavailable:
        pass
    else:
        raise AssertionError("incomplete S3 config must be reported as unavailable")


def test_delete_refuses_prefix_other_than_expected(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_RESEARCH_S3_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_S3_BUCKET", "aipoc")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("PORTFOLIO_RESEARCH_S3_SECRET_KEY", "sk")
    artifact = {
        "storage": "s3",
        "s3": {
            "bucket": "aipoc",
            "prefix": "portfolio-research/",
        },
    }

    with patch.object(s3mod, "_s3_client") as client_factory:
        stats = s3mod.delete_artifact_prefix(
            artifact,
            expected_prefix="portfolio-research/job-1/abcdef12/",
        )

    assert stats["removed"] == []
    assert any("refused unsafe S3 prefix" in item for item in stats["errors"])
    client_factory.assert_not_called()
