"""Pure tests for admin subscription plan form validation and persistence payloads."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "Backend/fastapi/routes/api_routes.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
WANTED = {"_normalize_plan_cap", "_plan_payload"}
NODES = [
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name in WANTED
]


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


NAMESPACE = {"re": re, "HTTPException": HTTPException}
exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), "exec"), NAMESPACE)


class PlanAdminPayloadTests(unittest.TestCase):
    def test_all_plan_fields_are_returned_for_tracking_collection_write(self):
        payload = NAMESPACE["_plan_payload"]({
            "name": "HD Plus",
            "days": "30",
            "price": "69",
            "currency": "INR",
            "daily_cap_gb": "50",
            "monthly_cap": "Unlimited",
        })
        self.assertEqual(payload, {
            "name": "HD Plus", "days": 30, "price": 69.0, "currency": "INR",
            "daily_cap_gb": 50, "monthly_cap": "Unlimited",
        })

    def test_monthly_cap_is_forced_to_unlimited_for_current_release(self):
        payload = NAMESPACE["_plan_payload"]({
            "name": "Current Plan", "days": 30, "price": 69, "currency": "INR",
            "daily_cap_gb": 0, "monthly_cap": 0,
        })
        self.assertEqual(payload["daily_cap_gb"], 0)
        self.assertEqual(payload["monthly_cap"], "Unlimited")

    def test_unlimited_and_decimal_caps_are_normalized(self):
        payload = NAMESPACE["_plan_payload"]({
            "name": "Flexible", "days": 45, "price": 99.5, "currency": "USD",
            "daily_cap_gb": "12.5", "monthly_cap": "unlimited",
        })
        self.assertEqual(payload["daily_cap_gb"], 12.5)
        self.assertEqual(payload["monthly_cap"], "Unlimited")


    def test_finite_monthly_cap_input_is_ignored(self):
        payload = NAMESPACE["_plan_payload"]({
            "name": "Basic", "days": 90, "price": 179, "currency": "INR",
            "daily_cap_gb": 70, "monthly_cap": "500",
        })
        self.assertEqual(payload["monthly_cap"], "Unlimited")

    def test_invalid_plan_name_validity_price_currency_and_caps_are_rejected(self):
        base = {
            "name": "Plan", "days": 30, "price": 69, "currency": "INR",
            "daily_cap_gb": 50, "monthly_cap": "Unlimited",
        }
        invalid = [
            {**base, "name": ""},
            {**base, "days": 0},
            {**base, "price": -1},
            {**base, "currency": "RUPEES"},
            {**base, "daily_cap_gb": -1},
        ]
        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(HTTPException):
                    NAMESPACE["_plan_payload"](item)


if __name__ == "__main__":
    unittest.main()
