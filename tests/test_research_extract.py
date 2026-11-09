"""Main-content extraction on saved pages: the content stays, the chrome goes."""
from __future__ import annotations

from research_fakes import fixture

from services.research.extract import clean_lines, detect_language, extract, normalize_date


def test_news_article():
    page = extract(fixture("news_article.html"), "https://news.example.com/business/port-strike-ends")
    text = page.text
    assert page.title == "Port strike ends after 12 days as workers accept pay deal"
    assert page.published.startswith("2026-10-30T08:15")
    assert page.lang == "en" and page.site_name == "Example News"
    assert page.canonical == "https://news.example.com/business/port-strike-ends"
    # the story, in order, with its structure
    assert text.startswith("Dock workers at the port of Northhaven")
    assert "## What the deal includes" in text
    assert "- A 9% pay rise backdated to July" in text
    assert "“pleased that an agreement has been reached”" in text  # entities decoded
    assert text.rstrip().endswith("in the run-up to the holiday season.")
    # boilerplate gone
    for junk in ("cookie", "Accept all", "Share on", "Subscribe", "Advertisement", "Related stories",
                 "Strike talks collapse", "All rights reserved", "Privacy policy", "World", "dataLayer",
                 "display:flex", "By Jane Doe"):
        assert junk not in text, junk


def test_docs_page():
    page = extract(fixture("docs_page.html"), "https://docs.example.org/library/json.html")
    text = page.text
    assert text.startswith("# json — JSON encoder and decoder")
    assert "JSON (JavaScript Object Notation) is a lightweight data interchange format" in text
    assert "```\n>>> import json\n>>> json.dumps(['foo', {'bar': ('baz', None, 1.0, 2)}])" in text
    assert "## Basic Usage" in text
    assert "| JSON | Python |" in text and "| object | dict |" in text
    for junk in ("Skip to main content", "Table of Contents", "\nmodules", "index\n", "Go\n", "Copyright",
                 "Found a bug", "searchtools", "Encoders and Decoders"):
        assert junk not in text, junk


def test_forum_thread_keeps_every_post():
    page = extract(fixture("forum_thread.html"), "https://forum.example.net/t/starter")
    text = page.text
    assert page.published.startswith("2026-09-14")
    assert "# How do I keep my sourdough starter alive while on holiday?" in text
    assert "I am going away for three weeks" in text           # the question
    assert "put it in the fridge in a jar with a loose lid" in text  # answer 1
    assert "Dried starter keeps for months" in text            # answer 2
    assert "> Make a dried backup" in text
    for junk in ("Reply", "Quote", "Log in", "Sign up", "Popular threads", "Best flour", "Next",
                 "Powered by", "Home"):
        assert junk not in text, junk


def test_cleaning_rules():
    raw = "Real sentence one.\n\nShare on Facebook\nReal sentence one.\nWe use cookies to improve this site.\n" \
          "​Zero​ width\xa0space\n|\n---\nAdvertisement\n"
    cleaned = clean_lines(raw)
    assert cleaned.count("Real sentence one.") == 1
    assert "Share" not in cleaned and "cookies" not in cleaned and "Advertisement" not in cleaned
    assert "Zero width space" in cleaned and "---" not in cleaned


def test_language_and_dates():
    assert detect_language("", "pt-BR") == "pt"
    assert detect_language("Le gouvernement a annoncé que la réforme des retraites est une priorité pour les mois "
                           "qui viennent et que le texte sera présenté dans la semaine.") == "fr"
    assert detect_language("भारत के प्रधानमंत्री सरकार के प्रमुख हैं") == "hi"
    assert normalize_date("2026-10-30T08:15:00Z").startswith("2026-10-30T08:15:00")
    assert normalize_date("Fri, 25 Sep 2026 02:10:00 GMT").startswith("2026-09-25")
    assert normalize_date("October 30, 2026") == "2026-10-30"
    assert normalize_date("not a date") == ""


def test_extract_never_raises_on_garbage():
    for markup in ("", "<<<>>>", "<html><body><p>unclosed <b>bold <i>x", "\x00\x01 plain text", "<table><tr><td>"):
        page = extract(markup)
        assert isinstance(page.text, str)
    assert "unclosed bold x" in extract("<html><body><p>unclosed <b>bold <i>x").text
