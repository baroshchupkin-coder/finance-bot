import unittest

from miniapp_dashboard import build_dashboard, normalize_username, user_can_manage


def request_row(
    request_id,
    project,
    status,
    payer_tag="@payer",
    category="Команда",
    payment_message_id="",
):
    row = [""] * 27
    row[0] = str(request_id)
    row[3] = project
    row[4] = f"Получатель {request_id}"
    row[5] = "1000 сом"
    row[6] = "Комментарий"
    row[7] = status
    row[8] = "-1004000000000"
    row[13] = payer_tag
    row[15] = "-1003000000000"
    row[20] = "-1004000000000"
    row[21] = str(100 + request_id)
    row[22] = category
    row[23] = "2026-08-17"
    row[25] = payment_message_id
    return row


class MiniAppDashboardTests(unittest.TestCase):
    def setUp(self):
        self.projects = [
            ["project", "payment_chat_id", "payer_tag", "approval_chat_id", "approver_tag"],
            ["ОР", "-1003", "@Payer", "-1004", "@Approver"],
        ]

    def test_username_normalization(self):
        self.assertEqual(normalize_username(" @KirillVorontcov "), "kirillvorontcov")

    def test_approver_sees_only_pending_assigned_invoices(self):
        rows = [["ID"], request_row(1, "ОР", "На согласовании"), request_row(2, "ОР", "Согласован")]
        dashboard = build_dashboard(rows, self.projects, "approver")
        self.assertEqual([item["request_id"] for item in dashboard["approvals"]], ["1"])

    def test_payer_sees_only_dispatched_unpaid_invoices(self):
        rows = [
            ["ID"],
            request_row(1, "ОР", "Согласован", payment_message_id="501"),
            request_row(2, "ОР", "Согласован"),
            request_row(3, "ОР", "Оплачено", payment_message_id="503"),
            request_row(4, "ОР", "Согласован", category="Такси", payment_message_id="504"),
        ]
        dashboard = build_dashboard(rows, self.projects, "@PAYER")
        self.assertEqual([item["request_id"] for item in dashboard["payments"]], ["1"])
        self.assertEqual(dashboard["payments"][0]["message_link"], "https://t.me/c/3000000000/501")

    def test_actions_require_current_assignment_and_status(self):
        pending = request_row(1, "ОР", "На согласовании")
        payment = request_row(2, "ОР", "Согласован", payment_message_id="502")
        self.assertTrue(user_can_manage(pending, self.projects, "approver", "approval"))
        self.assertFalse(user_can_manage(pending, self.projects, "someone", "approval"))
        self.assertTrue(user_can_manage(payment, self.projects, "payer", "payment"))
        payment[7] = "Оплачено"
        self.assertFalse(user_can_manage(payment, self.projects, "payer", "payment"))

    def test_historical_payer_still_gets_tab_after_project_mapping_changes(self):
        projects = [
            self.projects[0],
            ["ОР", "-1003", "@NewPayer", "-1004", "@Approver"],
        ]
        rows = [["ID"], request_row(5, "ОР", "Согласован", "@OldPayer", payment_message_id="505")]

        dashboard = build_dashboard(rows, projects, "oldpayer")

        self.assertTrue(dashboard["can_pay"])
        self.assertEqual([item["request_id"] for item in dashboard["payments"]], ["5"])


if __name__ == "__main__":
    unittest.main()
