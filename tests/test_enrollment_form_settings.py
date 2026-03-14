"""Tests for enrollment form settings and public dropdown filtering."""

import pytest

from models import User
from utils import BRANCH_CHOICES


def _ensure_superadmin(session):
    user = User.query.filter_by(email='enroll-settings-sa@test.local').first()
    if not user:
        user = User(
            name='Enroll Settings SA',
            email='enroll-settings-sa@test.local',
            password_hash='x',
            is_superadmin=True,
            is_approved=True,
            role='superadmin',
        )
        session.add(user)
        session.flush()
    return user


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def test_enrollment_settings_page_and_alias_load(client, db_session, app_instance):
    user = _ensure_superadmin(db_session)
    _login(client, user.id)

    resp1 = client.get('/admin/settings/enrollment-form')
    resp2 = client.get('/admin/enrollments/settings')

    assert resp1.status_code == 200
    assert resp2.status_code == 200


def test_enrollment_settings_filter_public_form_dropdowns(client, db_session, app_instance):
    branches = BRANCH_CHOICES()
    if len(branches) < 2:
        pytest.skip('Need at least two branches to validate filtering behavior')

    user = _ensure_superadmin(db_session)
    _login(client, user.id)

    allowed_branch = branches[0]
    disallowed_branch = branches[1]
    allowed_year = 'year7'
    disallowed_year = 'year12'

    post_resp = client.post(
        '/admin/settings/enrollment-form',
        data={
            'branches': [allowed_branch],
            'year_groups': [allowed_year],
        },
        follow_redirects=True,
    )
    assert post_resp.status_code == 200

    resp = client.get('/enroll')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8', errors='ignore')

    assert allowed_branch in html
    assert disallowed_branch not in html
    assert 'Year 7' in html
    assert 'Year 12' not in html
