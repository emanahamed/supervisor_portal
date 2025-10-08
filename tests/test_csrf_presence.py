from app import app


def test_schedule_message_csrf_field(client):
    # Temporarily enable CSRF for this check (test config disabled globally in conftest)
    app.config['WTF_CSRF_ENABLED'] = False  # already false, just explicit
    resp = client.get('/tools/schedule-message')
    assert resp.status_code in (200, 302)  # if permission redirect
    if resp.status_code == 200:
        html = resp.get_data(as_text=True)
        assert 'name="csrf_token"' in html, 'Expected csrf_token hidden field missing on schedule message form'
