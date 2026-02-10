from flask_wtf import FlaskForm
from wtforms import (BooleanField, DateField, DecimalField, FieldList,
                     FileField, FloatField, FormField, HiddenField,
                     IntegerField, PasswordField, SelectField,
                     SelectMultipleField, StringField, SubmitField,
                     TextAreaField, TimeField)
from wtforms.validators import (DataRequired, Email, Length, NumberRange,
                                Optional)
from wtforms.widgets import CheckboxInput, ListWidget

# Branch choices are injected at runtime from the Branch table (views should set SelectField.choices)
ISSUE_STATUS_CHOICES = [("Pending","Pending"),("In Progress","In Progress"),("Resolved","Resolved")]
ISSUE_CRITICALITY_CHOICES = [("Minor","Minor"),("Significant","Significant"),("Medium","Medium"),("Critical","Critical")]
ISSUE_URGENCY_CHOICES = [("Low","Low"),("Medium","Medium"),("High","High")]
TODO_STATUS_CHOICES = [("Pending","Pending"),("Done","Done")]
RESOURCE_TYPE_CHOICES = [
    ("Laptop","Laptop"),
    ("Walkie Talkie","Walkie Talkie"),
    ("Tablet","Tablet"),
    ("Electronic","Electronic"),
    ("Science Lab","Science Lab"),
    ("Other","Other"),
]
RESOURCE_STATUS_CHOICES = [("functional","Functional"),("need_repair","Need Repair"),("lost","Lost"),("archived","Archived")]
STAFF_INVOICE_STATUS_CHOICES = [("Pending","Pending"),("Approved","Approved"),("Rejected","Rejected")]

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
    # Name split
    first_name = StringField("First name", validators=[Optional(), Length(max=120)])
    last_name = StringField("Last name", validators=[Optional(), Length(max=120)])
    # Either provide full name or first+last; DataRequired kept to ensure legacy flows continue
    name = StringField("Full name", validators=[DataRequired()])
    # department choices will be injected dynamically in view (distinct existing depts + blank)
    department = SelectField("Department", validators=[Optional()], choices=[])
    email = StringField("Email", validators=[Optional(), Email()])
    phone = StringField("Phone", validators=[Optional()])
    dob = DateField("Date of birth", validators=[Optional()])
    # age computed server-side
    gender = SelectField("Gender", choices=[('', ''), ('male','Male'), ('female','Female'), ('non_binary','Non-binary'), ('prefer_not','Prefer not to say')], validators=[Optional()])
    relationship_status = SelectField("Relationship status", choices=[('', ''), ('single','Single'), ('married','Married'), ('civil_partnership','Civil partnership'), ('divorced','Divorced'), ('widowed','Widowed'), ('other','Other')], validators=[Optional()])
    national_insurance = StringField("National Insurance Number", validators=[Optional(), Length(max=40)])
    company_id = SelectField("Company", coerce=int, validators=[Optional()], choices=[])
    whitechapel_machine_id = StringField("Whitechapel Machine ID", validators=[Optional(), Length(max=120)])
    east_ham_machine_id = StringField("East Ham Machine ID", validators=[Optional(), Length(max=120)])
    stratford_machine_id = StringField("Stratford Machine ID", validators=[Optional(), Length(max=120)])
    docklands_machine_id = StringField("Docklands Machine ID", validators=[Optional(), Length(max=120)])
    # Ensure data is always a list (avoids NoneType membership tests in template)
    branches = SelectMultipleField("Branch(es)", choices=[], validators=[Optional()], default=[])
    access_code = StringField("Access Code", validators=[Optional(), Length(min=6, max=6)])
    active = BooleanField("Active", default=True)
    # Employment
    salary_per_hour = DecimalField("Salary per hour", validators=[Optional()], places=2)
    salary_notes = TextAreaField("Salary notes", validators=[Optional(), Length(max=2000)])
    employment_type = SelectField("Employment type", choices=[('', ''), ('permanent','Permanent'), ('temporary','Temporary'), ('contract','Contract'), ('zero_hours','Zero-hours')], validators=[Optional()])
    joining_date = DateField("Joining date", validators=[Optional()])
    # Medical
    medical_condition = SelectField("Medical condition", choices=[('', ''), ('none','None'), ('asthma','Asthma'), ('diabetes','Diabetes'), ('epilepsy','Epilepsy'), ('other','Other')], validators=[Optional()])
    medical_condition_other = StringField("If other, specify", validators=[Optional(), Length(max=255)])
    # Address
    address_line1 = StringField("Address line 1", validators=[DataRequired(), Length(max=255)])
    address_line2 = StringField("Address line 2", validators=[Optional(), Length(max=255)])
    town = StringField("Town/City", validators=[Optional(), Length(max=120)])
    region = StringField("State / Province / Region", validators=[Optional(), Length(max=120)])
    country = StringField("Country", validators=[DataRequired(), Length(max=120)])
    postcode = StringField("Postcode", validators=[DataRequired(), Length(max=40)])
    address_lookup_id = StringField("Address lookup id", validators=[Optional(), Length(max=255)])
    # Emergency contact
    emergency_first_name = StringField("Emergency first name", validators=[DataRequired(), Length(max=120)])
    emergency_last_name = StringField("Emergency last name", validators=[DataRequired(), Length(max=120)])
    emergency_mobile = StringField("Emergency mobile", validators=[DataRequired(), Length(max=50)])
    emergency_email = StringField("Emergency email", validators=[Optional(), Email(), Length(max=255)])
    emergency_relation = StringField("Relation with staff", validators=[Optional(), Length(max=80)])
    # Bank details
    bank_name_on_account = StringField("Name on account", validators=[DataRequired(), Length(max=255)])
    bank_name = StringField("Name of bank", validators=[DataRequired(), Length(max=255)])
    bank_sort_code = StringField("Sort code", validators=[Optional(), Length(max=20)])
    bank_account_number = StringField("Account number", validators=[DataRequired(), Length(max=40)])
    # DBS
    dbs_number = StringField("DBS number", validators=[Optional(), Length(max=120)])
    dbs_start_date = DateField("DBS start date", validators=[Optional()])
    dbs_expiry_date = DateField("DBS expiry date", validators=[Optional()])
    # choices populated in views; only staff with admin-like roles should be listed
    dbs_checked_by_id = SelectField("DBS checked by", coerce=int, validators=[Optional()], choices=[])
    # Photo upload handled in template via name="photo"
    # Use FileField so uploaded FileStorage objects are handled correctly
    photo = FileField("Photo", validators=[Optional()])
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
            ('tutor','Tutor'),
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
    # Populated dynamically in view from distinct Staff/Availability departments
    department = SelectField("Department", validators=[Optional()], choices=[])
    branches = SelectMultipleField("Branch(es)", choices=[], validators=[Optional()], default=[])
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
    branch = SelectField("Branch", choices=[], validators=[Optional()])
    action_taken = TextAreaField("Action Taken", validators=[Optional(), Length(max=5000)])
    submit = SubmitField("Save")


class MeetingForm(FlaskForm):
    participant_id = SelectField("Meeting With", coerce=int, validators=[DataRequired()])
    agenda = StringField("Agenda / Reason", validators=[DataRequired(), Length(max=500)])
    date = DateField("Date", validators=[DataRequired()])
    time = StringField("Time (HH:MM)", validators=[DataRequired(), Length(min=4, max=5)])
    # Optional: link a student to the meeting (for emailing/reminders)
    student_id = SelectField("Student", coerce=int, validators=[Optional()], choices=[])
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
    payment_method = SelectField(
        "Payment Method",
        choices=[("", "-- Select --"), ("card", "Card"), ("cash", "Cash"), ("bank_transfer", "Bank Transfer")],
        validators=[Optional()],
    )
    sub_total = DecimalField("Sub-total", validators=[DataRequired()], places=2)
    total = DecimalField("Total", validators=[DataRequired()], places=2)
    status = SelectField("Status", choices=[('PAID','PAID'),('UNPAID','UNPAID')], validators=[DataRequired()])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=2000)])
    submit = SubmitField("Save Invoice")


import calendar
import datetime


class StaffInvoiceForm(FlaskForm):
    # Invoice name removed per UI decision; month uses names and year defaults to current year
    month = SelectField(
        "Month",
        coerce=int,
        validators=[DataRequired()],
        choices=[(i, calendar.month_name[i]) for i in range(1, 13)],
    )
    year = IntegerField(
        "Year",
        validators=[DataRequired(), NumberRange(min=2000, max=2100)],
        default=datetime.date.today().year,
    )
    amount = DecimalField("Amount", validators=[Optional()], places=2)
    # Line items
    class ItemForm(FlaskForm):
        class Meta:
            csrf = False
        date = DateField("Date", validators=[DataRequired()])
        day = StringField("Day", validators=[Optional()])
        branch = SelectField("Branch", choices=[], validators=[Optional()])
        hours = DecimalField("Hours Worked", places=2, validators=[DataRequired()])
        description = StringField("Description", validators=[Optional(), Length(max=400)])
        rate = DecimalField("Rate/Hour", places=2, validators=[DataRequired()])
        amount = DecimalField("Amount", places=2, validators=[Optional()])
    items = FieldList(FormField(ItemForm), min_entries=1)
    # Actions
    save_draft = SubmitField("Save as Draft")
    submit_invoice = SubmitField("Submit Invoice")


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


# ---------------- Course Enrollment: Product & Discount Forms ---------------- #
class ProductForm(FlaskForm):
    name = StringField("Course Name", validators=[DataRequired(), Length(max=255)])
    description = TextAreaField("Description", validators=[Optional()])
    price = DecimalField("Price (£)", validators=[DataRequired(), NumberRange(min=0)], places=2)
    thumbnail_url = StringField("Thumbnail URL", validators=[Optional(), Length(max=500)])
    date = DateField("Course Date", validators=[Optional()])
    venue = StringField("Venue", validators=[Optional(), Length(max=255)])
    time = StringField("Time", validators=[Optional(), Length(max=100)])
    instructor = StringField("Instructor", validators=[Optional(), Length(max=200)])
    active = BooleanField("Active (visible on enrollment form)", default=True)
    submit = SubmitField("Save Product")


class EnrollmentDiscountForm(FlaskForm):
    discount_amount = DecimalField(
        "Discount Amount (£)",
        validators=[DataRequired(), NumberRange(min=0)],
        places=2,
        description="Fixed amount off when cart has even number of products"
    )
    submit = SubmitField("Save Discount Settings")


# ---------------- Award Ceremonies ---------------- #
class AwardCeremonyForm(FlaskForm):
    name = StringField("Event Name", validators=[DataRequired(), Length(max=255)])
    date = DateField("Event Date", validators=[DataRequired()])
    venue = StringField("Venue", validators=[Optional(), Length(max=255)])
    address = TextAreaField("Venue Address", validators=[Optional(), Length(max=500)])
    time = StringField("Time", validators=[Optional(), Length(max=100)])
    active = BooleanField("Active (open for public registration)", default=True)
    submit = SubmitField("Save Event")


# ---------------- Resource Management ---------------- #
class ResourceForm(FlaskForm):
    type = SelectField("Resource Type", choices=RESOURCE_TYPE_CHOICES, validators=[DataRequired()])
    type_other = StringField("If Other, specify", validators=[Optional(), Length(max=120)])
    branch = SelectField("Branch", choices=[], validators=[DataRequired()])
    name = StringField("Resource Name", validators=[Optional(), Length(max=120)])  # server will auto-generate if blank
    status = SelectField("Status", choices=RESOURCE_STATUS_CHOICES, validators=[DataRequired()], default='functional')
    submit = SubmitField("Save")


class ResourceBulkForm(FlaskForm):
    type = SelectField("Resource Type", choices=RESOURCE_TYPE_CHOICES, validators=[DataRequired()])
    type_other = StringField("If Other, specify", validators=[Optional(), Length(max=120)])
    branch = SelectField("Branch", choices=[], validators=[DataRequired()])
    status = SelectField("Status", choices=RESOURCE_STATUS_CHOICES, validators=[DataRequired()], default='functional')
    quantity = IntegerField("Quantity", validators=[DataRequired(), NumberRange(min=1, max=500)], default=1)
    submit = SubmitField("Create Resources")
