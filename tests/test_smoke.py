"""Smoke tests: the package imports, and logging actually emits JSON.

If either of these fails, nothing else in the suite is worth running.
"""

import json
import logging

import newsbot
from newsbot.logging_setup import configure_logging


def test_package_imports():
    assert newsbot is not None


def test_json_formatter_emits_parseable_json(capsys):
    configure_logging(level="INFO")
    logger = logging.getLogger("newsbot.test")
    logger.info("hello world", extra={"topic": "palworld", "count": 3})

    captured = capsys.readouterr()
    line = captured.out.strip().splitlines()[-1]
    record = json.loads(line)

    assert record["msg"] == "hello world"
    assert record["level"] == "INFO"
    assert record["logger"] == "newsbot.test"
    assert "ts" in record
    assert record["topic"] == "palworld"
    assert record["count"] == 3
