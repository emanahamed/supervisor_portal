import pytest

from forms import StaffForm
from models import Branch
from utils import BRANCH_CHOICES, ensure_form_branch_choices


def test_branch_choices_and_form_population(app_instance, db_session):
    # Ensure DB is clean for branches
    Branch.query.delete()
    db_session.commit()

    # Seed branches
    names = ["Whitechapel", "East Ham", "Stratford", "Docklands"]
    for n in names:
        db_session.add(Branch(name=n))
    db_session.commit()

    out = BRANCH_CHOICES()
    assert isinstance(out, list)
    for n in names:
        assert n in out

    # Create a form instance and run the safety helper
    form = StaffForm()
    ensure_form_branch_choices(form)
    # After population branches field should have choices
    assert getattr(form, 'branches', None) is not None
    assert len(form.branches.choices) >= len(names)
