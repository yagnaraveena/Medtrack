from flask import Flask, render_template, request, flash, redirect, url_for, session, g
from datetime import timedelta, datetime
import json
import os
import uuid
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import boto3

# ---------------------------------------
# Load environment variables
# ---------------------------------------
load_dotenv()

# ---------------------------------------
# Flask init
# ---------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=1)

# ---------------------------------------
# DynamoDB & SNS
# ---------------------------------------
AWS_REGION = os.environ.get('AWS_REGION_NAME', 'us-east-1')
USERS_TABLE = os.environ.get('USERS_TABLE_NAME', 'MedTrackUsers')
PATIENTS_TABLE = os.environ.get('PATIENTS_TABLE_NAME', 'MedTrackPatients')
DOCTORS_TABLE = os.environ.get('DOCTORS_TABLE_NAME', 'MedTrackDoctors')
APPOINTMENTS_TABLE = os.environ.get('APPOINTMENTS_TABLE_NAME', 'MedTrackAppointments')
PRESCRIPTIONS_TABLE = os.environ.get('PRESCRIPTIONS_TABLE_NAME', 'MedTrackPrescriptions')

try:
    aws_session = boto3.session.Session()
    dynamodb = aws_session.resource("dynamodb", region_name=AWS_REGION)
    sns = aws_session.client("sns", region_name=AWS_REGION)
    SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
except Exception as e:
    print(f"DynamoDB/SNS init failed: {e}")
    dynamodb = None
    sns = None
    SNS_TOPIC_ARN = None

# ---------------------------------------
# SNS Notification
# ---------------------------------------
def publish_to_sns(message, subject="MedTrack Notification"):
    if sns and SNS_TOPIC_ARN:
        try:
            sns.publish(TopicArn=SNS_TOPIC_ARN, Message=message, Subject=subject)
        except Exception as e:
            print(f"SNS publish error: {e}")
    else:
        print("SNS not configured, skipping publish.")

# ---------------------------------------
# Local fallback DB (commented out for deployment)
# ---------------------------------------
# local_db = {
#     "users": {},
#     "patients": {},
#     "doctors": {},
#     "appointments": {},
#     "prescriptions": {}
# }

# ---------------------------------------
# Helpers
# ---------------------------------------
def get_table(name):
    if dynamodb:
        return dynamodb.Table(name)
    return None

# ----------------------------------------
# File Paths
# ----------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "user.json")
PATIENTS_FILE = os.path.join(BASE_DIR, "patients.json")
DOCTORS_FILE = os.path.join(BASE_DIR, "doctors.json")
APPOINTMENTS_FILE = os.path.join(BASE_DIR, "appointments.json")

# ----------------------------------------
# Utility Functions
# ----------------------------------------
def load_data(file):
    if not os.path.exists(file):
        return []
    with open(file, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def save_data(file, data):
    with open(file, 'w') as f:
        json.dump(data, f, indent=4)

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_email' not in session:
            flash("Login required", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

@app.before_request
def load_logged_in_user():
    email = session.get('user_email')
    g.user = None
    if email:
        users = load_data(USERS_FILE)
        for user in users:
            if user['email'] == email:
                g.user = user
                break

# ----------------------------------------
# Routes
# ----------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        account_type = request.form.get('accountType')
        name = request.form.get('name', '').strip()
        age = request.form.get('age')
        gender = request.form.get('gender')
        contact = request.form.get('contact')
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if password != confirm_password:
            flash("Passwords do not match!", "danger")
            return render_template('signup.html')

        users = load_data(USERS_FILE)
        if any(user['email'] == email for user in users):
            flash("Email already registered.", "warning")
            return redirect(url_for('login'))

        new_user = {
            'accountType': account_type,
            'name': name,
            'age': age,
            'gender': gender,
            'contact': contact,
            'email': email,
            'password': generate_password_hash(password)
        }

        users.append(new_user)
        save_data(USERS_FILE, users)

        publish_to_sns(f"New user registered: {name} ({account_type})", "Signup Alert")

        session['user_email'] = email
        flash("Signup successful!", "success")
        return redirect(url_for('login'))

    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        password = request.form.get('password')
        users = load_data(USERS_FILE)

        for user in users:
            if user['email'] == email and check_password_hash(user['password'], password):
                session['user_email'] = email
                flash("Login successful!", "success")
                return redirect(url_for('dashboard'))

        flash('Invalid email or password.', 'danger')
        return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', user=g.user)

@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))

@app.route('/patientform', methods=['GET', 'POST'])
@login_required
def patientform():
    if request.method == 'POST':
        if not g.user or not isinstance(g.user, dict):
            flash("Something went wrong. Please login again.", "danger")
            return redirect(url_for('logout'))

        patient_data = {
            'email': g.user.get('email'),
            'name': g.user.get('name'),
            'age': request.form.get('age'),
            'gender': request.form.get('gender'),
            'contact': request.form.get('contact'),
            'address': request.form.get('address'),
            'bloodGroup': request.form.get('bloodGroup'),
            'medicalHistory': request.form.get('medicalHistory'),
            'prescriptions': []
        }

        if not all([patient_data['name'], patient_data['age'], patient_data['gender'],
                    patient_data['contact'], patient_data['address'], patient_data['bloodGroup']]):
            flash("All required fields must be filled.", "danger")
            return redirect(url_for('patientform'))

        patients = load_data(PATIENTS_FILE)
        patients = [p for p in patients if p.get('email') != g.user.get('email')]
        patients.append(patient_data)
        save_data(PATIENTS_FILE, patients)

        flash("Patient details saved!", "success")
        return redirect(url_for('patientdashboard'))

    return render_template('patientform.html')

@app.route('/patientdashboard')
@login_required
def patientdashboard():
    patients = load_data(PATIENTS_FILE)
    patient = next((p for p in patients if p.get('email') == g.user['email']), None)

    if not patient:
        flash("No patient data found. Please fill out the form.", "danger")
        return redirect(url_for('patientform'))

    prescriptions = patient.get('prescriptions', [])
    return render_template('patientdashboard.html', patient=patient, prescriptions=prescriptions)

@app.route('/doctorform', methods=['GET', 'POST'])
@login_required
def doctorform():
    if request.method == 'POST':
        doctor_data = {
            'email': g.user['email'],
            'name': g.user['name'],
            'specialization': request.form.get('specialization'),
            'experience': request.form.get('experience'),
            'qualification': request.form.get('qualification'),
            'availability': request.form.get('availability'),
            'contact': request.form.get('contact'),
            'address': request.form.get('address')
        }

        if not all(doctor_data.values()):
            flash("All fields are required.", "danger")
            return redirect(url_for('doctorform'))

        doctors = load_data(DOCTORS_FILE)
        doctors = [d for d in doctors if d['email'] != g.user['email']]
        doctors.append(doctor_data)
        save_data(DOCTORS_FILE, doctors)

        flash("Doctor details saved!", "success")
        return redirect(url_for('doctordashboard'))

    return render_template('doctorform.html')

@app.route('/doctordashboard')
@login_required
def doctordashboard():
    doctors = load_data(DOCTORS_FILE)
    doctor = next((d for d in doctors if d['email'] == g.user['email']), None)

    if not doctor:
        flash("No doctor data found. Please fill out the form.", "danger")
        return redirect(url_for('doctorform'))

    return render_template('doctordashboard.html', doctor=doctor)

@app.route('/bookanappointment', methods=['GET', 'POST'])
@login_required
def bookanappointment():
    if request.method == 'POST':
        appointment_data = {
            'patientName': request.form.get('patientName'),
            'doctorName': request.form.get('doctorName'),
            'date': request.form.get('date'),
            'time': request.form.get('time'),
            'reason': request.form.get('reason')
        }

        if not all(appointment_data.values()):
            flash("All fields are required.", "danger")
            return redirect(url_for('bookanappointment'))

        appointments = load_data(APPOINTMENTS_FILE)
        appointments.append(appointment_data)
        save_data(APPOINTMENTS_FILE, appointments)

        flash("Appointment booked successfully!", "success")
        return redirect(url_for('bookanappointment'))

    appointments = load_data(APPOINTMENTS_FILE)
    return render_template('bookanappointment.html', appointments=appointments)

# ----------------------------------------
# Run the app
# ----------------------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0',port=5000,debug=True)