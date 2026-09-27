"""Tests for newsbot.useragent: the one User-Agent builder every httpx client shares."""

from __future__ import annotations

import logging

from newsbot.useragent import build_user_agent, user_agent_headers, warn_if_contact_unset


def test_default_falls_back_to_repo_url_with_contact_unset():
    ua = build_user_agent({})
    assert ua.startswith("discord-newsbot/")
    assert "github.com" in ua
    assert "contact unset" in ua


def test_contact_env_var_is_honored():
    ua = build_user_agent({"NEWSBOT_CONTACT": "mailto:owner@example.com"})
    assert "(+mailto:owner@example.com)" in ua
    assert "contact unset" not in ua


def test_user_agent_headers_has_exactly_the_header_ua_builds():
    headers = user_agent_headers({"NEWSBOT_CONTACT": "https://example.com/contact"})
    assert headers == {
        "User-Agent": build_user_agent({"NEWSBOT_CONTACT": "https://example.com/contact"})
    }


def test_warn_if_contact_unset_logs_a_warning_when_unset(caplog):
    with caplog.at_level(logging.WARNING, logger="newsbot.useragent"):
        warn_if_contact_unset({})
    assert any("NEWSBOT_CONTACT" in record.message for record in caplog.records)


def test_warn_if_contact_unset_is_silent_when_set(caplog):
    with caplog.at_level(logging.WARNING, logger="newsbot.useragent"):
        warn_if_contact_unset({"NEWSBOT_CONTACT": "https://example.com/contact"})
    assert caplog.records == []
