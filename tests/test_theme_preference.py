from app import app, db
from models import User


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)

def test_quick_theme_update_persists(db_session):
    with app.app_context():
        # Create a user for this test only
        user = User(name='Theme User', email='themeuser@example.com', password_hash='x', is_approved=True)
        db.session.add(user)
        db.session.flush()
        uid = user.id
        assert user.theme_preference in (None, 'system', 'light', 'dark')
    with app.test_client() as client:
        _login(client, uid)
        # Send quick update to dark
        resp = client.post('/profile', data={'_theme_update':'1','theme_pref':'dark'})
        assert resp.status_code == 204
        with app.app_context():
            user = db.session.get(User, uid)
            assert user.theme_preference == 'dark'
        # Switch to system
        resp = client.post('/profile', data={'_theme_update':'1','theme_pref':'system'})
        assert resp.status_code == 204
        with app.app_context():
            user = db.session.get(User, uid)
            assert user.theme_preference == 'system'
        # Invalid value falls back to system
        resp = client.post('/profile', data={'_theme_update':'1','theme_pref':'unsupported'})
        assert resp.status_code == 204
        with app.app_context():
            user = db.session.get(User, uid)
            assert user.theme_preference == 'system'
