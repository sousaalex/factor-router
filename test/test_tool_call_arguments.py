from __future__ import annotations

import unittest

from src.gateway.proxy import _normalize_tool_call_arguments


class TestNormalizeToolCallArguments(unittest.TestCase):
    def test_none_becomes_empty_object(self) -> None:
        self.assertEqual(_normalize_tool_call_arguments(None), "{}")

    def test_empty_string_becomes_empty_object(self) -> None:
        self.assertEqual(_normalize_tool_call_arguments(""), "{}")

    def test_dict_is_serialized(self) -> None:
        self.assertEqual(_normalize_tool_call_arguments({"city": "Lisbon"}), '{"city": "Lisbon"}')

    def test_valid_json_string_is_kept(self) -> None:
        self.assertEqual(_normalize_tool_call_arguments('{"city":"Lisbon"}'), '{"city":"Lisbon"}')

    def test_invalid_json_string_becomes_empty_object(self) -> None:
        self.assertEqual(_normalize_tool_call_arguments('{"city":'), "{}")


if __name__ == "__main__":
    unittest.main()
