from datetime import datetime


def _ensure_perm(app):
    from models import Permission, RolePermission
    with app.app_context():
        if not Permission.query.filter_by(key='manage_staff').first():
            p = Permission(key='manage_staff', description='Manage staff records')
            app.db.session.add(p)  # type: ignore[attr-defined]
            app.db.session.commit()
        # role permissions seeded centrally; superadmin bypass is already in place


def test_convert_active_staff_creates_user_and_forces_reset(client, db_session, app, login_superadmin):
    _ensure_perm(app)
    from models import Staff, User

    # Setup one active staff with email
    s = Staff(name='Active Staff', email='active@example.com', department='Maths', branch='Whitechapel', active=True)
    db_session.add(s)
    db_session.commit()

    # Convert
    resp = client.post('/api/staff/convert-to-users', json={'ids': [s.id]})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert data['created'] == 1

    # Validate user
    u = User.query.filter_by(email='active@example.com').first()
    assert u is not None
    assert u.is_approved is True
    assert u.is_active is True
    # role should be tutor
    assert (u.role or '') == 'tutor'
    # force password reset flag must be set
    assert getattr(u, 'force_password_reset', False) is True

    # Staff should now be mapped
    s2 = Staff.query.get(s.id)
    assert s2.user_id == u.id


def test_inactive_staff_is_skipped(client, db_session, app, login_superadmin):
    _ensure_perm(app)
    from models import Staff
    s = Staff(name='Inactive Staff', email='inactive@example.com', department='Science', branch='Stratford', active=False)
    db_session.add(s)
    db_session.commit()

    resp = client.post('/api/staff/convert-to-users', json={'ids': [s.id]})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    # created 0, and skipped contains an entry with reason inactive
    assert data['created'] == 0
    reasons = {item['reason'] for item in data.get('skipped', [])}
    assert 'inactive' in reasons


def test_duplicate_email_requires_mapping(client, db_session, app, login_superadmin):
    _ensure_perm(app)
    from models import Staff, User

    # Existing user with email
    u = User(name='Existing', email='dupe@example.com', password_hash='x', is_approved=True)
    db_session.add(u)
    db_session.commit()
    # Staff with same email, active
    s = Staff(name='Dupe Staff', email='dupe@example.com', active=True)
    db_session.add(s)
    db_session.commit()

    resp = client.post('/api/staff/convert-to-users', json={'ids': [s.id]})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    # Should be skipped because a user exists already
    reasons = {item['reason'] for item in data.get('skipped', [])}
    assert 'user_exists' in reasons


def test_manual_mapping_sets_user_id(client, db_session, app, login_superadmin):
    _ensure_perm(app)
    from models import Staff, User
    u = User(name='Mapper', email='mapper@example.com', password_hash='x', is_approved=True)
    db_session.add(u)
    db_session.commit()
    s = Staff(name='To Map', email='mapper@example.com', active=True)
    db_session.add(s)
    db_session.commit()

    # GET page
    r_get = client.get(f'/staff/{s.id}/map-user')
    assert r_get.status_code == 200

    # POST mapping
    r_post = client.post(f'/staff/{s.id}/map-user', data={'user_id': str(u.id)}, follow_redirects=True)
    assert r_post.status_code == 200

    s2 = Staff.query.get(s.id)
    assert s2.user_id == u.id
