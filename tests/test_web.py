"""Tests for jobflow/web — Flask routes and API endpoints."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _test_store_path() -> Path:
    config_path = Path(os.environ["JOBFLOW_CONFIG"])
    return config_path.parent.parent / "data" / "ci" / "linkedin_jobs.json"


def _job(url: str, title: str, source: str = "linkedin") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "url": url,
        "company": "ExampleCo",
        "title": title,
        "location": "New York, NY",
        "score_pct": 82,
        "level": "Entry",
        "recommended": True,
        "status": "",
        "first_seen": now,
        "last_seen": now,
        "search_term": "software engineer",
        "source": source,
    }


def _write_store(jobs: list[dict]) -> None:
    _test_store_path().write_text(json.dumps({
        "jobs": {job["url"]: job for job in jobs},
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }))


class TestPageRoutes:
    """Test that all pages load correctly."""

    def test_root_redirects(self, client):
        r = client.get("/")
        assert r.status_code == 302
        assert "/linkedin" in r.headers["Location"]

    def test_linkedin_page(self, client):
        r = client.get("/linkedin")
        assert r.status_code == 200
        assert b"Job Feed" in r.data
        assert b"bulk-count" in r.data
        assert b"Open top 10" in r.data
        assert b"Delete top 10" in r.data
        assert b"AI Score" in r.data

    def test_health_endpoint(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["status"] == "ok"

    def test_removed_routes_404(self, client):
        """Removed pages should return 404 — the feed is the only page."""
        for path in ["/applications", "/dashboard", "/boards", "/scan", "/tailor"]:
            r = client.get(path)
            assert r.status_code == 404, f"{path} should be 404"


class TestLinkedInAPI:
    """Test LinkedIn jobs API endpoint."""

    def test_returns_html(self, client):
        r = client.get("/api/linkedin/jobs")
        assert r.status_code == 200
        assert r.content_type.startswith("text/html")

    def test_returns_count_headers(self, client):
        r = client.get("/api/linkedin/jobs")
        assert "X-Counts" in r.headers
        assert "X-Level-Counts" in r.headers
        assert "X-Time-Counts" in r.headers
        counts = json.loads(r.headers["X-Counts"])
        assert "All" in counts
        assert "Recommended" in counts

    def test_status_filter(self, client):
        r = client.get("/api/linkedin/jobs?status=Tracking")
        assert r.status_code == 200

    def test_level_filter(self, client):
        r = client.get("/api/linkedin/jobs?level=Entry")
        assert r.status_code == 200

    def test_search_filter(self, client):
        r = client.get("/api/linkedin/jobs?q=engineer")
        assert r.status_code == 200

    def test_sort_params(self, client):
        r = client.get("/api/linkedin/jobs?sort=score_pct&dir=desc")
        assert r.status_code == 200

    def test_time_filter(self, client):
        r = client.get("/api/linkedin/jobs?time=today&tz=240")
        assert r.status_code == 200

    def test_combined_filters(self, client):
        r = client.get("/api/linkedin/jobs?level=Entry&q=python&sort=score_pct&dir=desc&tz=240")
        assert r.status_code == 200

    def test_rows_include_bulk_selection_metadata(self, client):
        _write_store([_job("https://example.com/1", "Software Engineer")])
        r = client.get("/api/linkedin/jobs?time=")
        assert r.status_code == 200
        assert b'class="job-select"' in r.data
        assert b'data-key="https://example.com/1"' in r.data

    def test_bulk_delete_json_jobs(self, client):
        _write_store([
            _job("https://example.com/1", "Software Engineer One"),
            _job("https://example.com/2", "Software Engineer Two"),
            _job("https://example.com/3", "Software Engineer Three"),
        ])

        r = client.post(
            "/api/linkedin/jobs/bulk-delete",
            json={"keys": ["https://example.com/1", "https://example.com/2"]},
        )
        assert r.status_code == 200
        assert r.get_json() == {"requested": 2, "deleted": 2}

        feed = client.get("/api/linkedin/jobs?time=")
        assert b"Software Engineer One" not in feed.data
        assert b"Software Engineer Two" not in feed.data
        assert b"Software Engineer Three" in feed.data

    def test_bulk_delete_empty_keys_is_noop(self, client):
        r = client.post("/api/linkedin/jobs/bulk-delete", json={})
        assert r.status_code == 200
        assert r.get_json() == {"requested": 0, "deleted": 0}


class TestScanAPI:
    """Test scan endpoints that power the feed's Scan Now button."""

    def test_scan_status_is_json(self, client):
        r = client.get("/api/scan/status")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["running"] is False
        assert "relevant" in data


class TestAiScoreAPI:
    """Test local AI scoring runner endpoints."""

    def test_status_when_idle(self, client):
        r = client.get("/api/aiscore/status")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["running"] is False
        assert "scored" in data
        assert "log" in data
        assert "_process" not in data

    def test_trigger_rejects_unknown_engine(self, client):
        r = client.post("/api/aiscore/trigger", data={"engine": "gpt5"})
        assert r.status_code == 400
        assert "engine" in json.loads(r.data)["error"].lower()

    def test_trigger_defaults_to_gemma_and_reports_missing_setup(self, client, monkeypatch):
        import jobflow.ai_llama as ai_llama
        monkeypatch.setattr(ai_llama, "missing_setup", lambda: "llama-server not found.")
        r = client.post("/api/aiscore/trigger", data={})
        assert r.status_code == 400
        assert "llama-server" in json.loads(r.data)["error"]

    def test_feed_offers_gemma_first(self, client):
        html = client.get("/linkedin").data.decode()
        assert 'data-engine="gemma"' in html
        assert html.index('data-engine="gemma"') < html.index('data-engine="claude"')

    def test_trigger_rejects_bad_numbers(self, client):
        r = client.post("/api/aiscore/trigger", data={"engine": "claude", "hours": "abc"})
        assert r.status_code == 400

    def test_cancel_when_idle_is_safe(self, client):
        r = client.post("/api/aiscore/cancel")
        assert r.status_code == 200
        assert json.loads(r.data)["running"] is False


class TestRemovedFeatures:
    """Tracking, boards, scanner page, and tailor were removed."""

    def test_stats_endpoint_removed(self, client):
        assert client.get("/api/stats").status_code == 404

    def test_track_endpoint_removed(self, client):
        assert client.post("/api/scan/track", data={}).status_code == 404

    def test_status_patch_removed(self, client):
        r = client.patch(
            "/api/linkedin/jobs/https%3A%2F%2Fexample.com%2F1/status",
            data={"status": "Tracking"},
        )
        assert r.status_code in (404, 405)

    def test_boards_api_removed(self, client):
        assert client.get("/api/boards/jobs").status_code == 404
        assert client.post("/api/boards/jobs/bulk-delete", json={}).status_code == 404

    def test_tailor_api_removed(self, client):
        assert client.post("/api/tailor/generate", data={}).status_code == 404
        assert client.get("/api/tailor/status/abc").status_code == 404

    def test_file_serve_removed(self, client):
        assert client.get("/api/file/scan_results.json").status_code == 404
