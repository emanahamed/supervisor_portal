from flask_wtf import FlaskForm
from wtforms import (BooleanField, DateField, FloatField, HiddenField,
                     PasswordField, SelectField, SelectMultipleField,
                     StringField, SubmitField, TextAreaField, TimeField)
from wtforms.validators import (DataRequired, Email, Length, NumberRange,
                                Optional)
from wtforms.widgets import CheckboxInput, ListWidget

BRANCH_CHOICES = [("Whitechapel","Whitechapel"),("East Ham","East Ham"),("Stratford","Stratford"),("Docklands","Docklands")]
ISSUE_STATUS_CHOICES = [("Pending","Pending"),("In Progress","In Progress"),("Resolved","Resolved")]
ISSUE_CRITICALITY_CHOICES = [("Minor","Minor"),("Significant","Significant"),("Medium","Medium"),("Critical","Critical")]
ISSUE_URGENCY_CHOICES = [("Low","Low"),("Medium","Medium"),("High","High")]
TODO_STATUS_CHOICES = [("Pending","Pending"),("Done","Done")]

class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Sign in")

class RegisterForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=6)])
    submit = SubmitField("Create account")

class StaffForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired()])
    # department choices will be injected dynamically in view (distinct existing depts + blank)
    department = SelectField("Department", validators=[Optional()], choices=[])
    email = StringField("Email", validators=[Optional(), Email()])
    phone = StringField("Phone", validators=[Optional()])
    # Ensure data is always a list (avoids NoneType membership tests in template)
    branches = SelectMultipleField("Branch(es)", choices=BRANCH_CHOICES, validators=[Optional()], default=[])
    active = BooleanField("Active", default=True)
    submit = SubmitField("Save")

class CycleForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired()])
    start_date = DateField("Start date", validators=[Optional()])
    end_date = DateField("End date", validators=[Optional()])
    submit = SubmitField("Save")

class ObservationForm(FlaskForm):
    cycle_id = HiddenField(validators=[DataRequired()])
    staff_id = HiddenField(validators=[DataRequired()])
    date = DateField("Date", validators=[DataRequired()])
    score = FloatField("Score", validators=[DataRequired(), NumberRange(min=0)])
    submit = SubmitField("Save")

class UserProfileForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    role = SelectField("Role", choices=[('staff','staff'),('observer','observer'),('lead','lead'),('superadmin','superadmin')])
    is_approved = BooleanField("Approved")
    is_superadmin = BooleanField("Superadmin")
    password = PasswordField("New Password", validators=[Optional(), Length(min=6)])
    submit = SubmitField("Update Profile")

class AvailabilityForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=200)])
    department = StringField("Department", validators=[Optional(), Length(max=120)])
    branches = SelectMultipleField("Branch(es)", choices=BRANCH_CHOICES, validators=[Optional()], default=[])
    days = TextAreaField("Days / Time Slots", validators=[Optional(), Length(max=1000)])
    subjects = TextAreaField("Subjects", validators=[Optional(), Length(max=1000)])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=2000)])
    submit = SubmitField("Save")


class IssueForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired(), Length(max=200)])
    details = TextAreaField("Details", validators=[Optional(), Length(max=5000)])
    status = SelectField("Status", choices=ISSUE_STATUS_CHOICES, validators=[DataRequired()])
    criticality = SelectField("Criticality", choices=ISSUE_CRITICALITY_CHOICES, validators=[DataRequired()])
    urgency = SelectField("Urgency", choices=ISSUE_URGENCY_CHOICES, validators=[DataRequired()])
    branch = SelectField("Branch", choices=[(b,b) for b,_ in BRANCH_CHOICES], validators=[Optional()])
    action_taken = TextAreaField("Action Taken", validators=[Optional(), Length(max=5000)])
    submit = SubmitField("Save")


class MeetingForm(FlaskForm):
    participant_id = SelectField("Meeting With", coerce=int, validators=[DataRequired()])
    agenda = StringField("Agenda / Reason", validators=[DataRequired(), Length(max=500)])
    date = DateField("Date", validators=[DataRequired()])
    time = StringField("Time (HH:MM)", validators=[DataRequired(), Length(min=4, max=5)])
    student_name = StringField("Student Name", validators=[Optional(), Length(max=200)])
    parent_name = StringField("Parent Name", validators=[Optional(), Length(max=200)])
    outcome = TextAreaField("Outcome / Notes", validators=[Optional(), Length(max=5000)])
    submit = SubmitField("Save")


class TodoForm(FlaskForm):
    description = StringField("Description", validators=[DataRequired(), Length(max=400)])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=10000)])
    actions_taken = TextAreaField("Actions Taken", validators=[Optional(), Length(max=10000)])
    criticality = SelectField("Criticality", choices=ISSUE_CRITICALITY_CHOICES, validators=[DataRequired()])
    urgency = SelectField("Urgency", choices=ISSUE_URGENCY_CHOICES, validators=[DataRequired()])
    status = SelectField("Status", choices=TODO_STATUS_CHOICES, validators=[DataRequired()], default='Pending')
    due_date = DateField("Due Date", validators=[Optional()])
    assigned_to_id = SelectField("Assign To", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Save Task")
