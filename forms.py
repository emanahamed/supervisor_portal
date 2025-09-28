from flask_wtf import FlaskForm
from wtforms import (BooleanField, DateField, FloatField, HiddenField,
                     PasswordField, SelectField, SelectMultipleField,
                     StringField, SubmitField)
from wtforms.validators import (DataRequired, Email, Length, NumberRange,
                                Optional)
from wtforms.widgets import CheckboxInput, ListWidget

BRANCH_CHOICES = [("Whitechapel","Whitechapel"),("East Ham","East Ham"),("Stratford","Stratford"),("Docklands","Docklands")]

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
