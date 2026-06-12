"""API Key 常量时间校验单测（production-hardening #13）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.middleware import _is_valid_api_key  # noqa: E402

KEYS = {"key-aaa", "key-bbb"}


def test_valid_key_accepted():
    assert _is_valid_api_key("key-aaa", KEYS) is True
    assert _is_valid_api_key("key-bbb", KEYS) is True


def test_invalid_key_rejected():
    assert _is_valid_api_key("key-ccc", KEYS) is False
    assert _is_valid_api_key("key-aa", KEYS) is False  # 前缀不算命中
    assert _is_valid_api_key("", KEYS) is False


def test_empty_keyset_rejects_all():
    assert _is_valid_api_key("anything", set()) is False
