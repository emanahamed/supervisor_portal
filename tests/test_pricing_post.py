def test_pricing_update_requires_permission(client, app_instance):
    # Without login -> redirect
    resp = client.post('/tools/pricing', data={'year3_5_1':'123'})
    assert resp.status_code in (301,302)


def test_pricing_update_round_trip(client, app_instance, db_session):
    # Create superadmin user with permission (superadmin bypass)
    from models import Setting, User, db
    with app_instance.app_context():
        u = User(name='Pricing Admin', email='pricing@test.local', password_hash='x', is_superadmin=True, is_approved=True)
        db.session.add(u); db.session.flush()
        uid = u.id
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
    resp = client.post('/tools/pricing', data={
        'year3_5_1':'111.11',
        'registration_fee':'77.77'
    }, follow_redirects=True)
    assert resp.status_code == 200
    # Settings persisted
    with app_instance.app_context():
        tm = Setting.query.filter_by(key='tuition_matrix').first()
        assert tm is not None and '111.11' in tm.value
        reg = Setting.query.filter_by(key='registration_fee').first()
        assert reg is not None and '77.77' in reg.value
