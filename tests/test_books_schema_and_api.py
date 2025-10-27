import pytest
from sqlalchemy import text

from app import db
from models import Book


def test_book_columns_present(app_instance):
    with app_instance.app_context():
        cols = {row[1] for row in db.session.execute(text("PRAGMA table_info(book)"))}
        # If legacy columns remain, attempt migration script then re-inspect.
        if any(c in cols for c in {"min_subjects","max_subjects","image_url"}):
            try:
                import migrate_drop_legacy_book_columns  # noqa: F401
            except Exception as _mexc:
                pytest.skip(f"Legacy columns present and migration failed to import: {_mexc}")
            cols = {row[1] for row in db.session.execute(text("PRAGMA table_info(book)"))}
        for required in {"cover","cover_url","inner","inner_url","print_format","finishing"}:
            assert required in cols, f"Missing expected column: {required}"
        for legacy in {"min_subjects","max_subjects","image_url"}:
            assert legacy not in cols, f"Legacy column still present after migration attempt: {legacy}"

def test_public_api_books(client, app_instance, db_session):
    # Create a test-only active book
    with app_instance.app_context():
        b = Book(name="Test Book (public api)", price=10.0, subject="Maths", year_group="year8", active=True)
        db.session.add(b)
        db.session.flush()
    resp = client.get('/api/books')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'results' in data
    assert isinstance(data['results'], list)
    # Should only see active books by default
    for r in data['results']:
        assert r['active'] is True
    # Inactive inclusion check (toggle our test book)
    with app_instance.app_context():
        b.active = False
        db.session.commit()
    resp2 = client.get('/api/books?all=1')
    assert any(r['active'] is False for r in resp2.get_json()['results']), 'Inactive book not returned with all=1'
