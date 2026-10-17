import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock
from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location("backfill", Path(__file__).with_name("backfill_history_display_index.py"))
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.ddb = MagicMock()
        self.item = {"pk": {"S": "STATION#garden"}, "sk": {"S": "OBS#old"}}

    def test_only_adds_missing_index_attribute(self):
        self.assertEqual("updated", backfill.add_index_key(self.ddb, "Weather", self.item, "DISPLAY#time#OBS#old"))
        kwargs = self.ddb.update_item.call_args.kwargs
        self.assertEqual(self.item, kwargs["Key"])
        self.assertEqual("SET history_sort = :history_sort", kwargs["UpdateExpression"])
        self.assertIn("attribute_exists(pk)", kwargs["ConditionExpression"])
        self.assertIn("attribute_not_exists(history_sort)", kwargs["ConditionExpression"])

    def test_concurrent_identical_backfill_is_safe(self):
        self.ddb.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.ddb.get_item.return_value = {"Item": {"history_sort": {"S": "expected"}}}
        self.assertEqual("already_indexed", backfill.add_index_key(self.ddb, "Weather", self.item, "expected"))

    def test_does_not_overwrite_conflicting_index_key_or_recreate_deleted_item(self):
        self.ddb.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        for current in ({}, {"Item": {"history_sort": {"S": "different"}}}):
            self.ddb.get_item.return_value = current
            with self.assertRaises(RuntimeError):
                backfill.add_index_key(self.ddb, "Weather", self.item, "expected")

    def test_other_dynamodb_errors_stop_migration(self):
        self.ddb.update_item.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "UpdateItem")
        with self.assertRaises(ClientError):
            backfill.add_index_key(self.ddb, "Weather", self.item, "expected")


if __name__ == "__main__":
    unittest.main()
