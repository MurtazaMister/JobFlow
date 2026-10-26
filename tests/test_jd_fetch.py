"""JD extraction from apply pages (offline: parsing only, no network)."""

import json

from jobflow.jd_fetch import html_to_text, jd_from_html, jd_from_page_text

BODY = "<p>Build services.</p><ul><li>Python</li><li>Go</li></ul>" + "<p>More detail.</p>" * 20


def test_json_ld_job_posting():
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "name": "Acme"},
        {"@type": "JobPosting", "title": "SWE", "description": BODY},
    ]}
    html = f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>'
    jd = jd_from_html(html)
    assert jd.startswith("Build services.")
    assert "• Python" in jd


def test_page_text_fallback_starts_at_jd_heading():
    html = ("<html><nav>Home Jobs Login</nav><div><h2>Description</h2>" + BODY +
            "</div><footer>Privacy</footer></html>")
    jd = jd_from_page_text(html)
    assert jd.startswith("Description")
    assert "Login" not in jd and "Privacy" not in jd


def test_html_to_text_handles_encoded_html():
    assert html_to_text("&lt;p&gt;Hi&lt;/p&gt;&lt;p&gt;There&lt;/p&gt;") == "Hi\n There"
