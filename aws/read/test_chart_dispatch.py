import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))
from test_app import app
config = types.ModuleType('botocore.config')
config.Config = MagicMock()
sys.modules.setdefault('botocore.config', config)

class ChartDispatchTests(unittest.TestCase):
    def setUp(self):
        self.query = {'stationId': 'garden', 'from': '2026-01-01T00:00:00Z', 'to': '2026-07-01T00:00:00Z', 'view': 'chart', 'maxPoints': '800'}
        self.env = patch.dict(os.environ, {'AGGREGATE_HISTORY_ENABLED': 'true', 'ROLLUP_TABLE_NAME': 'rollups-test'})
        self.env.start()
        self.index = patch.object(app, 'HISTORY_INDEX_NAME', 'ObservationDisplayTimeIndex')
        self.index.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.index.stop)

    def call(self):
        return app.handle_get_history({'queryStringParameters': self.query})

    def test_disabled_gate_does_not_query_raw(self):
        with patch.dict(os.environ, {'AGGREGATE_HISTORY_ENABLED': 'false'}), patch.object(app, 'ddb') as ddb:
            result = self.call()
        self.assertEqual(503, result['statusCode'])
        ddb.query.assert_not_called()

    def test_dispatch_does_not_enter_legacy_sample_scan(self):
        helper = types.ModuleType('aggregate_history')
        helper.handle_chart_query = MagicMock(return_value={'ok': True, 'mode': 'aggregate', 'count': 1})
        self.query['sample'] = '800'
        with patch.dict(sys.modules, {'aggregate_history': helper}), patch.object(app.boto3, 'client') as client, patch.object(app, 'fetch_all_items_for_sampling') as old:
            result = self.call()
        self.assertEqual(200, result['statusCode'])
        old.assert_not_called()
        self.assertEqual(800, helper.handle_chart_query.call_args.args[-1])

    def test_budget_and_not_ready_errors_are_structured(self):
        helper = types.ModuleType('aggregate_history')
        for error in ('history_query_budget_exceeded', 'history_aggregates_not_ready'):
            helper.handle_chart_query = MagicMock(side_effect=ValueError(error))
            with patch.dict(sys.modules, {'aggregate_history': helper}), patch.object(app.boto3, 'client'):
                result = self.call()
            self.assertEqual(503, result['statusCode'])
            self.assertEqual(error, json.loads(result['body'])['error'])

    def test_invalid_budget_rejected_before_read(self):
        for budget in ('0', '2001', 'abc'):
            self.query['maxPoints'] = budget
            with patch.object(app.boto3, 'client') as client:
                result = self.call()
            self.assertEqual(400, result['statusCode'])
            client.assert_not_called()

if __name__ == '__main__':
    unittest.main()
