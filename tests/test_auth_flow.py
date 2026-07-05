"""Allowlist + access-request lifecycle."""
import auth_store


def test_unknown_user_files_request_then_admin_approves():
    auth_store.initialize_auth_db()
    user_id = 555001

    assert not auth_store.is_user_allowed(user_id)
    status = auth_store.create_access_request(
        user_id, display_name="New Friend", username="newfriend", chat_id=999,
    )
    assert status == "pending"
    # Re-asking doesn't duplicate
    assert auth_store.create_access_request(
        user_id, display_name="New Friend", username="newfriend", chat_id=999,
    ) == "pending"
    assert auth_store.pending_access_request_count() >= 1

    pending = auth_store.list_access_requests(status="pending")
    req = next(r for r in pending if r.telegram_user_id == user_id)
    resolved = auth_store.approve_access_request(req.request_id)
    assert resolved is not None and resolved.chat_id == 999
    assert auth_store.is_user_allowed(user_id)
    # Approving twice is a no-op
    assert auth_store.approve_access_request(req.request_id) is None


def test_denied_user_stays_denied():
    auth_store.initialize_auth_db()
    user_id = 555002
    auth_store.create_access_request(
        user_id, display_name="Rando", username=None, chat_id=1000,
    )
    req = next(
        r for r in auth_store.list_access_requests(status="pending")
        if r.telegram_user_id == user_id
    )
    resolved = auth_store.deny_access_request(req.request_id)
    assert resolved is not None
    assert not auth_store.is_user_allowed(user_id)
    # A later message from them reports 'denied', not a fresh pending request
    assert auth_store.create_access_request(
        user_id, display_name="Rando", username=None, chat_id=1000,
    ) == "denied"


def test_seed_claim_and_revoke():
    auth_store.initialize_auth_db()
    auth_store.add_allowed_user(555003, display_name="Revokee")
    assert auth_store.is_user_allowed(555003)
    row = next(u for u in auth_store.list_allowed_users() if u.telegram_user_id == 555003)
    assert auth_store.remove_allowed_user(row.row_id)
    assert not auth_store.is_user_allowed(555003)
