import json
import re
from datetime import date

from app import app, db
from models import (Observation, ObservationCycle, ObservationDetail, Staff,
                    User)


def test_mixed_prefixed_keys_render_checked(db_session):
    with app.app_context():
        # Create minimal related data inside the test transaction
        u = User(name='Tester', email='tester@example.com', password_hash='x', is_approved=True)
        s = Staff(name='Tutor A')
        c = ObservationCycle(title='Cycle 1')
        db.session.add_all([u, s, c])
        db.session.flush()
        # Observation and detail under test
        obs = Observation(cycle_id=c.id, staff_id=s.id, observer_id=u.id, date=date.today(), score=5.0)
        db.session.add(obs)
        db.session.flush()
        obs_id = obs.id
        weekly_raw = {
            'weekly_test_marked_on_time': True,
            'weekly_test_weekly_test_labelled_correctly': True,
            'suitable_difficulty_level': True,
            'weekly_test_weekly_test_weekly_test_was_of_appropriate_format': True,
        }
        detail = ObservationDetail(
            observation_id=obs_id,
            timeslot='9-11',
            weekly_test=json.dumps(weekly_raw),
            homework=json.dumps({}),
            classwork=json.dumps({}),
            org_mgmt=json.dumps({}),
        )
        db.session.add(detail)
        db.session.flush()
    def _login(client):
        with client.session_transaction() as sess:
            sess['_user_id'] = str(u.id)

    with app.test_client() as client:
        _login(client)
        r = client.get(f'/observations/extended/{obs_id}/edit')
        assert r.status_code == 200
        html = r.data.decode('utf-8')
        # Expect the four labels to be checked (look for input with name and checked attribute)
        labels = [
            'Weekly Test Was of Appropriate Format',
            'Suitable Difficulty Level',
            'Labelled Correctly',
            'Marked on Time'
        ]
        for label in labels:
            key_full = label.replace(' ', '_').lower()
            # Name attribute is prefix + '_' + key_full regardless of how raw was stored
            name_attr = f'weekly_test_{key_full}'
            pattern = rf'name="{re.escape(name_attr)}"[^>]*checked'
            assert re.search(pattern, html), f"Expected checkbox for {label} ({name_attr}) to be checked"
