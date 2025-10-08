from flask_wtf import FlaskForm
from wtforms import (BooleanField, DateField, DecimalField, FieldList,
                     FloatField, FormField, HiddenField, IntegerField,
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
    remember = BooleanField("Remember me", default=False)
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
    # Updated taxonomy: staff, supervisor, centre_manager, admin, superadmin
    role = SelectField(
        "Role",
        choices=[
            ('staff','Staff'),
            ('supervisor','Supervisor'),
            ('centre_manager','Centre Manager'),
            ('admin','Admin'),
            ('superadmin','Super Admin'),
        ]
    )
    is_approved = BooleanField("Approved")
    is_superadmin = BooleanField("Superadmin")
    theme_preference = SelectField("Theme Preference", choices=[('system','System'),('light','Light'),('dark','Dark')])
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


# ---------------- Appointment Scheduling ---------------- #
class AppointmentSlotForm(FlaskForm):
    superadmin_id = SelectField("Management Team Member", coerce=int, validators=[DataRequired()])
    date = DateField("Date", validators=[DataRequired()])
    start_time = TimeField("Start Time", validators=[DataRequired()])
    end_time = TimeField("End Time", validators=[DataRequired()])
    notes = StringField("Notes", validators=[Optional(), Length(max=255)])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Create Slot")


class AppointmentSlotBulkForm(FlaskForm):
    superadmin_id = SelectField("Management Team Member", coerce=int, validators=[DataRequired()])
    date = DateField("Date", validators=[DataRequired()])
    start_time = TimeField("Start Time", validators=[DataRequired()])
    end_time = TimeField("End Time", validators=[DataRequired()])
    duration_minutes = IntegerField("Duration (minutes)", validators=[DataRequired(), NumberRange(min=5, max=480)])
    notes = StringField("Notes", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Bulk Create")


class AppointmentSlotActionForm(FlaskForm):
    slot_id = HiddenField(validators=[DataRequired()])
    action = HiddenField(validators=[DataRequired()])
    submit = SubmitField()


class AppointmentBookingForm(FlaskForm):
    slot_id = SelectField("Available Slots", coerce=int, validators=[DataRequired()])
    name = StringField("Your Name", validators=[DataRequired(), Length(max=200)])
    student_ref = StringField("Student Name / ID", validators=[DataRequired(), Length(max=200)])
    reason = TextAreaField("Reason for Appointment", validators=[DataRequired(), Length(max=1000)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    phone = StringField("Phone", validators=[DataRequired(), Length(max=50)])
    language = HiddenField(validators=[Optional()])
    submit = SubmitField("Book Appointment")


class AppointmentBookingActionForm(FlaskForm):
    booking_id = HiddenField(validators=[DataRequired()])
    action = HiddenField(validators=[DataRequired()])
    submit = SubmitField()


# ---------------- Invoicing ---------------- #
class CompanyForm(FlaskForm):
    name = StringField("Company Name", validators=[DataRequired(), Length(max=200)])
    tagline = StringField("Tagline", validators=[Optional(), Length(max=200)])
    ofsted_reg_no = StringField("OFSTED Registration No", validators=[Optional(), Length(max=64)])
    address = TextAreaField("Address", validators=[Optional(), Length(max=400)])
    phone = StringField("Phone", validators=[Optional(), Length(max=64)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=255)])
    website = StringField("Website", validators=[Optional(), Length(max=255)])
    invoice_prefix = StringField("Invoice Prefix", validators=[Optional(), Length(max=20)])
    next_invoice_seq = StringField("Next Sequence", validators=[Optional(), Length(max=10)])
    payment_footer = StringField("Payment Footer", validators=[Optional(), Length(max=300)])
    logo = StringField("Logo")  # will treat as file input in template via name="logo"; kept simple
    submit = SubmitField("Save Company")


class InvoiceForm(FlaskForm):
    company_id = SelectField("Company", coerce=int, validators=[DataRequired()])
    parent_name = StringField("Parent Name", validators=[DataRequired(), Length(max=200)])
    parent_phone = StringField("Parent Phone", validators=[Optional(), Length(max=64)])
    parent_email = StringField("Parent Email", validators=[Optional(), Email(), Length(max=255)])
    parent_address = TextAreaField("Parent Address", validators=[Optional(), Length(max=400)])
    child_name = StringField("Child Cared For", validators=[DataRequired(), Length(max=200)])
    period_start = DateField("Period Start", validators=[DataRequired()])
    period_end = DateField("Period End", validators=[DataRequired()])
    invoice_date = DateField("Invoice Date", validators=[DataRequired()])
    due_date = DateField("Due Date", validators=[DataRequired()])
    sub_total = DecimalField("Sub-total", validators=[DataRequired()], places=2)
    total = DecimalField("Total", validators=[DataRequired()], places=2)
    status = SelectField("Status", choices=[('PAID','PAID'),('UNPAID','UNPAID')], validators=[DataRequired()])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=2000)])
    submit = SubmitField("Save Invoice")


class StudentForm(FlaskForm):
    student_id = StringField("Student ID", validators=[DataRequired(), Length(max=64)])
    name = StringField("Name", validators=[DataRequired(), Length(max=255)])
    type = StringField("Type", validators=[Optional(), Length(max=120)])
    year = StringField("Year", validators=[Optional(), Length(max=20)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=255)])
    phone = StringField("Phone", validators=[Optional(), Length(max=64)])
    address = TextAreaField("Address", validators=[Optional(), Length(max=2000)])
    academic = TextAreaField("Academic", validators=[Optional(), Length(max=5000)])
    status = StringField("Status", validators=[Optional(), Length(max=120)])
    submit = SubmitField("Save Student")


class PricingConfigForm(FlaskForm):
    """Form for dynamic tuition + registration + book pricing configuration."""
    year3_5_1 = DecimalField("Y3-5 1 Subject", validators=[Optional()])
    year3_5_2 = DecimalField("Y3-5 2 Subjects", validators=[Optional()])
    year3_5_3 = DecimalField("Y3-5 3 Subjects", validators=[Optional()])
    year6_7_1 = DecimalField("Y6-7 1 Subject", validators=[Optional()])
    year6_7_2 = DecimalField("Y6-7 2 Subjects", validators=[Optional()])
    year6_7_3 = DecimalField("Y6-7 3 Subjects", validators=[Optional()])
    year6_7_4 = DecimalField("Y6-7 4 Subjects", validators=[Optional()])
    year8_1 = DecimalField("Y8 1 Subj", validators=[Optional()])
    year8_2 = DecimalField("Y8 2 Subj", validators=[Optional()])
    year8_3 = DecimalField("Y8 3 Subj", validators=[Optional()])
    year8_4 = DecimalField("Y8 4 Subj", validators=[Optional()])
    year9_1 = DecimalField("Y9 1 Subj", validators=[Optional()])
    year9_2 = DecimalField("Y9 2 Subj", validators=[Optional()])
    year9_3 = DecimalField("Y9 3 Subj", validators=[Optional()])
    year9_4 = DecimalField("Y9 4 Subj", validators=[Optional()])
    year10_1 = DecimalField("Y10 1 Subj", validators=[Optional()])
    year10_2 = DecimalField("Y10 2 Subj", validators=[Optional()])
    year10_3 = DecimalField("Y10 3 Subj", validators=[Optional()])
    year10_4 = DecimalField("Y10 4 Subj", validators=[Optional()])
    year11_1 = DecimalField("Y11 1 Subj", validators=[Optional()])
    year11_2 = DecimalField("Y11 2 Subj", validators=[Optional()])
    year11_3 = DecimalField("Y11 3 Subj", validators=[Optional()])
    year11_4 = DecimalField("Y11 4 Subj", validators=[Optional()])
    alevel_1 = DecimalField("A-Level 1 Subj", validators=[Optional()])
    alevel_2 = DecimalField("A-Level 2 Subj", validators=[Optional()])
    alevel_3 = DecimalField("A-Level 3 Subj", validators=[Optional()])
    alevel_4 = DecimalField("A-Level 4 Subj", validators=[Optional()])
    registration_fee = DecimalField("Registration Fee", validators=[Optional()])
    # Stationery / optional item unit prices
    writing_book_price = DecimalField("Writing Book Price", validators=[Optional()])
    planner_price = DecimalField("Planner Price", validators=[Optional()])
    # Dynamic stationery items JSON (list of {key,label,price,default_qty})
    stationery_json = TextAreaField("Stationery Items JSON", validators=[Optional()])
    # Default deposit percent (applied to tuition unless overridden)
    deposit_percent = DecimalField("Default Deposit % of Tuition", validators=[Optional()])
    submit = SubmitField("Save Pricing")


YEAR_GROUP_CHOICES = [
    ('year3-5','Year 3-5'),
    ('year6-7','Year 6-7'),
    ('year8','Year 8'),
    ('year9','Year 9'),
    ('year10','Year 10'),
    ('year11','Year 11'),
    ('alevel','A-Level'),
]

class BookForm(FlaskForm):
    name = StringField("Book Name", validators=[DataRequired(), Length(max=255)])  # Book_Name
    subject = StringField("Subject", validators=[Optional(), Length(max=120)])
    year_group = StringField("Year", validators=[Optional(), Length(max=20)])  # simple year value
    price = DecimalField("Price", validators=[DataRequired()], places=2)
    cover = StringField("Cover", validators=[Optional(), Length(max=255)])
    cover_url = StringField("Cover URL", validators=[Optional(), Length(max=500)])
    inner = StringField("Inner", validators=[Optional(), Length(max=255)])
    inner_url = StringField("Inner URL", validators=[Optional(), Length(max=500)])
    print_format = StringField("Print Format", validators=[Optional(), Length(max=120)])
    finishing = StringField("Finishing", validators=[Optional(), Length(max=120)])
    active = BooleanField("Active", default=True)
    submit = SubmitField("Save Book")
