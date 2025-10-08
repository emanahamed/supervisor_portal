import json


def test_tuition_matrix_api(client):
    # Public endpoint (no auth) after adjustment
    resp = client.get('/api/tuition-matrix')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'matrix' in data
    assert 'registration_fee' in data
    assert isinstance(data['matrix'], dict)


def test_pricing_page_requires_auth(client):
    resp = client.get('/tools/pricing')
    assert resp.status_code in (301, 302)
    # Enrollment tool also requires auth
    resp2 = client.get('/tools/enroll')
    assert resp2.status_code in (301, 302)

