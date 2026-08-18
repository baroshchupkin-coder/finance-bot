from datetime import date


REQUEST_ID_COL = 0
PROJECT_COL = 3
TARGET_COL = 4
AMOUNT_COL = 5
COMMENT_COL = 6
STATUS_COL = 7
APPROVER_CHAT_ID_COL = 8
PAYER_TAG_COL = 13
PAYMENT_CHAT_ID_COL = 15
LAST_APPROVAL_CHAT_ID_COL = 20
LAST_APPROVAL_MESSAGE_ID_COL = 21
EXPENSE_CATEGORY_COL = 22
PAYMENT_DUE_DATE_COL = 23
PAYMENT_MESSAGE_ID_COL = 25

STATUS_PENDING_APPROVAL = "На согласовании"
STATUS_APPROVED = "Согласован"
TAXI_EXPENSE_CATEGORY = "Такси"


def cell(row, index, default=""):
    if index >= len(row):
        return default
    value = row[index]
    return value if value not in (None, "") else default


def normalize_username(value):
    return str(value or "").strip().lstrip("@").casefold()


def telegram_message_link(chat_id, message_id):
    chat_text = str(chat_id or "").strip()
    message_text = str(message_id or "").strip()
    if not chat_text or not message_text:
        return ""
    if chat_text.startswith("-100"):
        return f"https://t.me/c/{chat_text[4:]}/{message_text}"
    return ""


def project_settings_by_name(project_rows):
    result = {}
    for row in project_rows[1:]:
        project = normalize_username(cell(row, 0))
        if not project:
            continue
        result[project] = {
            "payer_tag": cell(row, 2),
            "approver_tag": cell(row, 4),
        }
    return result


def _sort_key(item):
    due_date = item.get("payment_due_date") or date.max.isoformat()
    request_id = item.get("request_id", "")
    try:
        numeric_id = int(request_id)
    except (TypeError, ValueError):
        numeric_id = 10**12
    return due_date, numeric_id


def invoice_item(row, stage):
    if stage == "approval":
        chat_id = cell(row, LAST_APPROVAL_CHAT_ID_COL) or cell(
            row, APPROVER_CHAT_ID_COL
        )
        message_id = cell(row, LAST_APPROVAL_MESSAGE_ID_COL)
    else:
        chat_id = cell(row, PAYMENT_CHAT_ID_COL)
        message_id = cell(row, PAYMENT_MESSAGE_ID_COL)

    return {
        "request_id": cell(row, REQUEST_ID_COL),
        "project": cell(row, PROJECT_COL),
        "target": cell(row, TARGET_COL),
        "amount": cell(row, AMOUNT_COL),
        "comment": cell(row, COMMENT_COL),
        "expense_category": cell(row, EXPENSE_CATEGORY_COL),
        "payment_due_date": cell(row, PAYMENT_DUE_DATE_COL),
        "message_link": telegram_message_link(chat_id, message_id),
    }


def build_dashboard(
    request_rows,
    project_rows,
    username,
    finance_viewer_usernames=(),
):
    normalized_user = normalize_username(username)
    finance_viewers = {
        normalize_username(value)
        for value in finance_viewer_usernames
        if normalize_username(value)
    }
    is_finance_viewer = normalized_user in finance_viewers
    projects = project_settings_by_name(project_rows)
    approvals = []
    payments = []
    can_approve = any(
        normalize_username(settings.get("approver_tag")) == normalized_user
        for settings in projects.values()
    )
    can_pay = is_finance_viewer or any(
        normalize_username(settings.get("payer_tag")) == normalized_user
        for settings in projects.values()
    )

    if not normalized_user:
        return {
            "approvals": approvals,
            "payments": payments,
            "can_approve": False,
            "can_pay": False,
            "is_finance_viewer": False,
        }

    for row in request_rows[1:]:
        project = projects.get(normalize_username(cell(row, PROJECT_COL)), {})
        status = cell(row, STATUS_COL)

        if (
            status == STATUS_PENDING_APPROVAL
            and normalize_username(project.get("approver_tag")) == normalized_user
        ):
            approvals.append(invoice_item(row, "approval"))

        is_dispatched_payment = (
            status == STATUS_APPROVED
            and cell(row, EXPENSE_CATEGORY_COL) != TAXI_EXPENSE_CATEGORY
            and cell(row, PAYMENT_MESSAGE_ID_COL)
        )
        assigned_payer = normalize_username(cell(row, PAYER_TAG_COL))
        if is_dispatched_payment and (
            is_finance_viewer or assigned_payer == normalized_user
        ):
            item = invoice_item(row, "payment")
            item["payer_tag"] = cell(row, PAYER_TAG_COL, "не указан")
            item["can_act"] = assigned_payer == normalized_user
            payments.append(item)

    approvals.sort(key=_sort_key)
    payments.sort(key=_sort_key)
    return {
        "approvals": approvals,
        "payments": payments,
        "can_approve": can_approve or bool(approvals),
        "can_pay": can_pay or bool(payments),
        "is_finance_viewer": is_finance_viewer,
    }


def user_can_manage(row, project_rows, username, stage):
    normalized_user = normalize_username(username)
    if not normalized_user:
        return False

    if stage == "approval":
        projects = project_settings_by_name(project_rows)
        project = projects.get(normalize_username(cell(row, PROJECT_COL)), {})
        return (
            cell(row, STATUS_COL) == STATUS_PENDING_APPROVAL
            and normalize_username(project.get("approver_tag")) == normalized_user
        )

    return (
        cell(row, STATUS_COL) == STATUS_APPROVED
        and cell(row, EXPENSE_CATEGORY_COL) != TAXI_EXPENSE_CATEGORY
        and bool(cell(row, PAYMENT_MESSAGE_ID_COL))
        and normalize_username(cell(row, PAYER_TAG_COL)) == normalized_user
    )
