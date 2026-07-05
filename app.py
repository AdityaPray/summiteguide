import os
import uuid
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from models import db, User, Mountain, Basecamp, Equipment, Ticket, RentalTransaction
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flasgger import Swagger
from datetime import timedelta, datetime
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from flask_mail import Mail, Message
import random
import midtransclient
from sqlalchemy import func

from flask import Blueprint, request, jsonify, session
from functools import wraps
from models import db, User, Mountain, Basecamp  # Sesuaikan dengan lokasi model Anda

# [DIUBAH] Import helper upload ke Supabase Storage (menggantikan photo.save() lokal)
from storage_service import upload_file_to_supabase

load_dotenv()
app = Flask(__name__)
admin_bp = Blueprint('admin', __name__)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv('EMAIL')  # Ganti email Anda
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')  # Gunakan App Password 16 karakter
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('EMAIL')

mail = Mail(app)

# [DIUBAH] UPLOAD_FOLDER tidak lagi dipakai untuk menyimpan file (Vercel filesystem
# read-only & /tmp tidak persist). Konfig ini dibiarkan ada untuk kompatibilitas
# kalau ada kode lain yang masih mereferensikannya, tapi TIDAK dipakai lagi untuk
# photo.save(). Semua upload sekarang lewat storage_service.upload_file_to_supabase().
UPLOAD_FOLDER = 'static/uploads/profiles/'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


# ==========================================
# INISIALISASI MIDTRANS SNAP
# ==========================================
snap = midtransclient.Snap(
    is_production=False,  # Ganti True jika nanti sudah rilis (Go-Live)
    server_key=os.getenv('MIDTRANS_SERVER_KEY'),
    client_key=os.getenv('MIDTRANS_CLIENT_KEY')
)

# ==========================================
# 1. KONFIGURASI SUPABASE & KEAMANAN VIA .ENV
# ==========================================
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('SUPABASE_DATABASE_URI')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_COOKIE_CSRF_PROTECT'] = False
app.secret_key = os.getenv('FLASK_SECRET_KEY')
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=7)

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

db.init_app(app)
jwt = JWTManager(app)

from apscheduler.schedulers.background import BackgroundScheduler
from weather_service import (
    update_all_mountains_weather, get_forecast_by_name, get_history_by_name
)


def serialize_user(user):
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "phone": user.phone,
        "profile_photo": user.profile_photo,
        "role": user.role,
        "status": user.status
    }


# ==========================================
# [DIUBAH] SCHEDULER CUACA — HANYA UNTUK LOCAL DEVELOPMENT
# ==========================================
# Vercel otomatis mengisi env var VERCEL=1 di runtime-nya. Serverless function
# tidak punya proses yang hidup terus-menerus, jadi BackgroundScheduler
# TIDAK bisa diandalkan di sana. Kalau terdeteksi jalan di Vercel, scheduler
# in-process ini tidak dinyalakan — sebagai gantinya pakai Vercel Cron Jobs
# yang memanggil endpoint /api/cron/update-weather (lihat di bawah).
IS_VERCEL = os.getenv('VERCEL') == '1'


def job_update_weather():
    with app.app_context():
        mountain_names = [m.name for m in Mountain.query.all()]
        update_all_mountains_weather(mountain_names)


if not IS_VERCEL:
    # Guard tambahan supaya scheduler tidak jalan dobel gara-gara
    # Flask debug reloader (2 proses sekaligus di localhost).
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            job_update_weather, 'interval', hours=3,
            id='weather_update_job', next_run_time=datetime.now()
        )
        scheduler.start()
        print("[SCHEDULER] Weather scheduler started (local mode)")


# ==========================================
# [BARU] ENDPOINT CRON UNTUK VERCEL
# ==========================================
# Didaftarkan di vercel.json (bagian "crons") supaya Vercel yang memanggil
# endpoint ini secara terjadwal, menggantikan BackgroundScheduler di atas.
CRON_SECRET = os.getenv('CRON_SECRET')


@app.route('/api/cron/update-weather', methods=['GET'])
def cron_update_weather():
    # Vercel Cron otomatis mengirim header "Authorization: Bearer <CRON_SECRET>"
    # kalau env var CRON_SECRET sudah diset di project Vercel Anda.
    # Ini mencegah orang lain sembarangan memicu endpoint ini dari luar.
    auth_header = request.headers.get('Authorization')
    if CRON_SECRET and auth_header != f"Bearer {CRON_SECRET}":
        return jsonify({"message": "Unauthorized"}), 401

    mountain_names = [m.name for m in Mountain.query.all()]
    update_all_mountains_weather(mountain_names)
    return jsonify({
        "message": "Update cuaca selesai",
        "total_gunung": len(mountain_names)
    }), 200

# --- KONFIGURASI SWAGGER LENGKAP ---
swagger_template = {
    "swagger": "2.0",
    "info": {
        "title": "StagingAI / Summit Guide API",
        "description": "Dokumentasi RESTful API untuk integrasi Backend Flask dengan Aplikasi Mobile (Flutter).",
        "version": "1.0.0"
    },
    "securityDefinitions": {
        "Bearer": {
            "type": "apiKey",
            "name": "Authorization",
            "in": "header",
            "description": "Masukkan token JWT dengan format: <b>Bearer &lt;token_anda&gt;</b>"
        }
    }
}
swagger = Swagger(app, template=swagger_template)


# ==========================================
# 2. WEB VIEW ROUTES (DASHBOARD ADMIN BROWSER)
# ==========================================

@app.route('/')
def home():
    if 'user_id' in session:
        return redirect(url_for('web_dashboard'))
    return redirect(url_for('web_login'))


@app.route('/login', methods=['GET', 'POST'])
def web_login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        user = User.query.filter_by(email=email).first()
        if user and user.password_hash and check_password_hash(user.password_hash, password):
            if user.status == 'active':
                session['user_id'] = user.id
                session['user_name'] = user.name
                session['user_role'] = user.role
                return redirect(url_for('web_dashboard'))
            else:
                flash('Akun Anda belum diverifikasi!', 'warning')
        else:
            flash('Email atau password salah!', 'danger')

    return render_template('login.html')


from flask_mail import Message
import random
from datetime import datetime, timedelta


@app.route('/register', methods=['GET', 'POST'])
def web_register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')

        role = 'pendaki'

        if not name or not email or not password:
            flash('Semua field wajib diisi!', 'danger')
            return redirect(url_for('web_register'))

        if User.query.filter_by(email=email).first():
            flash('Email sudah terdaftar!', 'danger')
            return redirect(url_for('web_register'))

        otp = str(random.randint(100000, 999999))
        expiry_time = datetime.now() + timedelta(minutes=10)

        new_user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            role=role,
            status='unverified',
            otp_code=otp,
            otp_expiry=expiry_time
        )

        try:
            db.session.add(new_user)
            db.session.commit()

            msg = Message("Kode Verifikasi Summit Guide", recipients=[email])
            msg.body = f"Halo {name},\n\nKode verifikasi Anda adalah: {otp}\nBerlaku selama 10 menit.\n\nTerima kasih."
            mail.send(msg)

            flash('Registrasi berhasil! Silakan cek email untuk OTP.', 'success')
            return redirect(url_for('view_verify_email', email=email))

        except Exception as e:
            db.session.rollback()
            print(f"Error Registrasi: {e}")
            flash('Terjadi kesalahan sistem, coba lagi.', 'danger')
            return redirect(url_for('web_register'))

    return render_template('register.html')


@app.route('/verify-email', methods=['GET'])
def view_verify_email():
    email = request.args.get('email')

    if not email:
        return redirect('/login')

    return render_template('otp_verivication.html', email=email)


@app.route('/api/auth/resend-otp', methods=['POST'])
def api_resend_otp():
    data = request.get_json()
    email = data.get('email')

    if not email:
        return jsonify({"message": "Email tidak valid"}), 400

    user = User.query.filter_by(email=email).first()

    if not user:
        return jsonify({"message": "User tidak ditemukan"}), 404

    if user.status != 'unverified':
        return jsonify({"message": "Akun ini sudah diverifikasi sebelumnya."}), 400

    new_otp = str(random.randint(100000, 999999))
    new_expiry = datetime.now() + timedelta(minutes=10)

    user.otp_code = new_otp
    user.otp_expiry = new_expiry
    db.session.commit()

    try:
        msg = Message("Kirim Ulang Kode OTP Summit Guide", recipients=[email])
        msg.body = f"Halo {user.name},\n\nAnda meminta pengiriman ulang kode verifikasi.\nKode OTP baru Anda adalah: {new_otp}\nBerlaku selama 10 menit.\n\nTerima kasih."
        mail.send(msg)
        return jsonify({"message": "OTP baru berhasil dikirim ke email Anda."}), 200
    except Exception as e:
        print(f"Error resend email: {e}")
        return jsonify({"message": "Gagal mengirim email. Coba lagi nanti."}), 500


@app.route('/profile', methods=['GET', 'POST'])
def user_profile():
    if 'user_id' not in session:
        flash('Silakan login terlebih dahulu.', 'warning')
        return redirect(url_for('web_login'))

    user = User.query.get(session['user_id'])

    if request.method == 'POST':
        action = request.form.get('action')

        # --- AKSI 1: UPDATE PROFIL & FOTO ---
        if action == 'update_profile':
            user.name = request.form.get('name')
            user.phone = request.form.get('phone')

            # [DIUBAH] Upload foto profil sekarang ke Supabase Storage,
            # bukan disimpan ke disk lokal (photo.save()) yang tidak
            # persist di Vercel.
            photo = request.files.get('profile_photo')
            if photo and photo.filename != '':
                uploaded_url = upload_file_to_supabase(photo, folder="profiles")
                if uploaded_url:
                    user.profile_photo = uploaded_url

            db.session.commit()
            session['user_name'] = user.name
            flash('Profil berhasil diperbarui!', 'success')

        # --- AKSI 2: GANTI PASSWORD ---
        elif action == 'update_password':
            old_password = request.form.get('old_password')
            new_password = request.form.get('new_password')

            if check_password_hash(user.password_hash, old_password):
                user.password_hash = generate_password_hash(new_password)
                db.session.commit()
                flash('Password berhasil diubah!', 'success')
            else:
                flash('Password lama salah!', 'danger')

        return redirect(url_for('user_profile'))

    basecamps = Basecamp.query.all()

    return render_template('profile.html', user=user, basecamps=basecamps)


@app.route('/profile/upgrade', methods=['POST'])
def upgrade_account():
    if 'user_id' not in session:
        return redirect(url_for('web_login'))

    user = User.query.get(session['user_id'])
    target_role = request.form.get('role')

    basecamp_id = request.form.get('basecamp_id')
    ktp_number = request.form.get('ktp_number')

    existing_ktp = User.query.filter(User.ktp_number == ktp_number, User.id != user.id).first()
    if existing_ktp:
        flash('Pengajuan Gagal: Nomor KTP sudah terdaftar pada sistem.', 'danger')
        return redirect(url_for('user_profile'))

    user.basecamp_id = basecamp_id
    user.ktp_number = ktp_number

    # [DIUBAH] Folder lokal UPLOAD_DOC_FOLDER tidak dipakai lagi untuk
    # menyimpan file — semua upload dokumen (KTP, surat tugas) sekarang
    # lewat Supabase Storage.

    # Simpan KTP (karena Vendor & Basecamp sama-sama butuh KTP)
    ktp_file = request.files.get('ktp_image')
    if ktp_file and ktp_file.filename != '':
        uploaded_url = upload_file_to_supabase(ktp_file, folder="documents")
        if uploaded_url:
            user.ktp_image = uploaded_url

    if target_role == 'vendor':
        user.shop_name = request.form.get('shop_name')
        user.bank_name = request.form.get('bank_name')
        user.bank_account = request.form.get('bank_account')
        user.role = 'vendor'

    elif target_role == 'basecamp_admin':
        # [DIUBAH] Upload surat tugas ke Supabase Storage
        surat_file = request.files.get('official_letter')
        if surat_file and surat_file.filename != '':
            uploaded_url = upload_file_to_supabase(surat_file, folder="documents")
            if uploaded_url:
                user.official_letter = uploaded_url

        user.role = 'basecamp_admin'

    user.status = 'pending'

    db.session.commit()

    flash('Pengajuan berhasil! Silakan tunggu verifikasi Super Admin.', 'success')
    return redirect(url_for('user_profile'))


@app.route('/dashboard')
def web_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('web_login'))

    user = User.query.get(session['user_id'])
    role = user.role
    data = {}

    if role == 'super_admin':
        data['total_tiket'] = db.session.query(func.count(Ticket.id)).filter(
            Ticket.payment_status == 'paid').scalar() or 0
        data['pendapatan_tiket'] = db.session.query(
            func.sum(Ticket.ticket_price * Ticket.quantity)
        ).filter(Ticket.payment_status == 'paid').scalar() or 0
        data['total_sewa'] = db.session.query(func.count(RentalTransaction.id)).filter(
            RentalTransaction.payment_status == 'paid').scalar() or 0
        data['pendapatan_sewa'] = db.session.query(
            func.sum(RentalTransaction.total_price)
        ).filter(RentalTransaction.payment_status == 'paid').scalar() or 0

    elif role == 'basecamp_admin':
        bc_id = user.basecamp_id
        data['total_tiket'] = db.session.query(func.count(Ticket.id)).filter(
            Ticket.basecamp_id == bc_id, Ticket.payment_status == 'paid').scalar() or 0
        data['pendapatan_tiket'] = db.session.query(
            func.sum(Ticket.ticket_price * Ticket.quantity)
        ).filter(Ticket.basecamp_id == bc_id, Ticket.payment_status == 'paid').scalar() or 0

    elif role == 'vendor':
        data['total_sewa'] = db.session.query(func.count(RentalTransaction.id)).join(
            Equipment, RentalTransaction.equipment_id == Equipment.id
        ).filter(Equipment.vendor_id == user.id, RentalTransaction.payment_status == 'paid').scalar() or 0
        data['pendapatan_sewa'] = db.session.query(
            func.sum(RentalTransaction.total_price)
        ).join(Equipment, RentalTransaction.equipment_id == Equipment.id
        ).filter(Equipment.vendor_id == user.id, RentalTransaction.payment_status == 'paid').scalar() or 0

    return render_template('dashboard.html', user=user, data=data)


@app.route('/logout')
def web_logout():
    session.clear()
    flash('Anda telah logout.', 'success')
    return redirect(url_for('web_login'))


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('user_role') != 'super_admin':
            return jsonify({"message": "Akses ditolak: Memerlukan role super_admin"}), 403
        return f(*args, **kwargs)
    return decorated_function


def basecamp_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('user_role') != 'basecamp_admin':
            flash('Akses ditolak: Halaman ini hanya untuk Admin Basecamp.', 'danger')
            return redirect(url_for('web_login'))
        return f(*args, **kwargs)
    return decorated_function


# --- TAMPILAN HALAMAN ADMIN ---

@app.route('/admin/vendors', methods=['GET'])
@admin_required
def view_vendors():
    pending_vendors = User.query.filter_by(role='vendor', status='pending').all()
    return render_template('vendors.html', vendors=pending_vendors)


@app.route('/admin/basecamps', methods=['GET'])
@admin_required
def view_basecamps():
    pending_basecamps = User.query.filter_by(role='basecamp_admin', status='pending').all()
    return render_template('basecamps.html', basecamps=pending_basecamps)


@app.route('/admin/gpx', methods=['GET'])
@admin_required
def view_gpx():
    unverified_mountains = Mountain.query.filter_by(is_verified=False).all()
    return render_template('gpx.html', mountains=unverified_mountains)


@admin_bp.route('/admin/mountains/<int:mountain_id>/verify-gpx', methods=['POST'])
@admin_required
def verify_gpx(mountain_id):
    mountain = Mountain.query.get_or_404(mountain_id)
    data = request.get_json()

    mountain.is_verified = data.get('is_verified', True)
    db.session.commit()
    return jsonify({"message": f"GPX untuk gunung {mountain.name} telah diverifikasi."})


@admin_bp.route('/admin/vendors/verify/<int:user_id>', methods=['POST'])
@admin_required
def verify_vendor(user_id):
    vendor = User.query.filter_by(id=user_id, role='vendor').first_or_404()

    vendor.status = 'active'
    db.session.commit()
    return jsonify({"message": f"Vendor {vendor.shop_name} telah diaktifkan."})


@admin_bp.route('/admin/basecamps/verify/<int:user_id>', methods=['POST'])
@admin_required
def verify_basecamp(user_id):
    user = User.query.filter_by(id=user_id, role='basecamp_admin').first_or_404()

    user.status = 'active'
    db.session.commit()
    return jsonify({"message": f"Admin Basecamp {user.name} telah diaktifkan."})


@admin_bp.route('/admin/pending-requests', methods=['GET'])
@admin_required
def get_pending_requests():
    pending_vendors = User.query.filter_by(role='vendor', status='pending').all()
    pending_basecamps = User.query.filter_by(role='basecamp_admin', status='pending').all()

    return jsonify({
        "vendors": [{"id": v.id, "shop": v.shop_name, "ktp": v.ktp_image} for v in pending_vendors],
        "basecamps": [{"id": b.id, "name": b.name, "letter": b.official_letter} for b in pending_basecamps]
    })


@app.route('/admin/mountains/add', methods=['GET', 'POST'])
@admin_required
def add_mountain():
    if request.method == 'POST':
        name = request.form.get('name')
        lat = request.form.get('latitude')
        lng = request.form.get('longitude')
        desc = request.form.get('description')

        if not name:
            flash('Nama gunung wajib diisi!', 'danger')
            return redirect(url_for('add_mountain'))

        # [DIUBAH] Upload GPX ke Supabase Storage, bukan disimpan ke disk lokal
        gpx_file = request.files.get('gpx_file')
        gpx_path = None

        if gpx_file and gpx_file.filename != '':
            gpx_path = upload_file_to_supabase(gpx_file, folder="gpx")

        new_mountain = Mountain(
            name=name,
            location_lat=float(lat) if lat else None,
            location_long=float(lng) if lng else None,
            description=desc,
            gpx_data=gpx_path,
            is_verified=True
        )

        db.session.add(new_mountain)
        db.session.commit()

        flash(f'Gunung {name} beserta jalur GPX berhasil ditambahkan!', 'success')
        return redirect(url_for('web_dashboard'))

    return render_template('add_mountain.html')


@app.route('/admin/basecamps/add', methods=['GET', 'POST'])
@admin_required
def add_basecamp():
    mountains = Mountain.query.all()

    if request.method == 'POST':
        name = request.form.get('name')
        mountain_id = request.form.get('mountain_id')
        daily_quota = request.form.get('daily_quota')

        if not name or not mountain_id:
            flash('Nama Basecamp dan Gunung wajib dipilih!', 'danger')
            return redirect(url_for('add_basecamp'))

        new_basecamp = Basecamp(
            name=name,
            mountain_id=int(mountain_id),
            daily_quota=int(daily_quota) if daily_quota else 0
        )

        db.session.add(new_basecamp)
        db.session.commit()

        flash(f'Basecamp {name} berhasil ditambahkan!', 'success')
        return redirect(url_for('web_dashboard'))

    return render_template('add_basecamp.html', mountains=mountains)


@app.route('/basecamp/manage', methods=['GET', 'POST'])
@basecamp_admin_required
def manage_basecamp():
    user = User.query.get(session['user_id'])

    basecamp = Basecamp.query.get(user.basecamp_id)

    if not basecamp:
        flash('Akun Anda belum terhubung dengan basecamp manapun. Hubungi Super Admin.', 'warning')
        return redirect(url_for('web_dashboard'))

    if request.method == 'POST':
        daily_quota = request.form.get('daily_quota')
        ticket_price = request.form.get('ticket_price')

        if not daily_quota or not ticket_price:
            flash('Semua bidang pengaturan wajib diisi!', 'danger')
            return redirect(url_for('manage_basecamp'))

        basecamp.daily_quota = int(daily_quota)
        basecamp.ticket_price = float(ticket_price)

        db.session.commit()
        flash('Pengaturan kuota dan tarif tiket berhasil diperbarui!', 'success')
        return redirect(url_for('manage_basecamp'))

    return render_template('basecamp/manage.html', basecamp=basecamp)


@app.route('/basecamp/tickets', methods=['GET'])
@basecamp_admin_required
def manage_tickets():
    user = User.query.get(session['user_id'])
    tickets = Ticket.query.filter_by(basecamp_id=user.basecamp_id).all()

    return render_template('basecamp/tickets.html', tickets=tickets)


@app.route('/basecamp/scanner', methods=['GET'])
@basecamp_admin_required
def web_scanner():
    return render_template('basecamp/scanner.html')


@app.route('/basecamp/tickets/update-status/<int:ticket_id>', methods=['POST'])
@basecamp_admin_required
def update_ticket_status(ticket_id):
    new_status = request.form.get('status')
    ticket = Ticket.query.get_or_404(ticket_id)

    user = User.query.get(session['user_id'])
    if ticket.basecamp_id != user.basecamp_id:
        flash('Akses ditolak!', 'danger')
        return redirect(url_for('manage_tickets'))

    ticket.status = new_status
    db.session.commit()

    flash(f'Status tiket berhasil diubah menjadi {new_status}!', 'success')
    return redirect(url_for('manage_tickets'))


def vendor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('user_role') != 'vendor':
            flash('Akses ditolak: Halaman ini khusus untuk Vendor.', 'danger')
            return redirect(url_for('web_dashboard'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/vendor/equipments', methods=['GET', 'POST'])
@vendor_required
def manage_equipments():
    user = User.query.get(session['user_id'])

    if request.method == 'POST':
        item_name = request.form.get('item_name')
        price = request.form.get('price_per_day')
        stock = request.form.get('stock')
        image_file = request.files.get('image_file')

        if not item_name or not price or not stock:
            flash('Harap isi semua data peralatan!', 'warning')
            return redirect(url_for('manage_equipments'))

        # [DIUBAH] Upload foto alat ke Supabase Storage, bukan disk lokal
        image_url = None
        if image_file and image_file.filename != '':
            image_url = upload_file_to_supabase(image_file, folder="equipments")

        new_equipment = Equipment(
            vendor_id=user.id,
            basecamp_id=user.basecamp_id,
            item_name=item_name,
            price_per_day=float(price),
            stock=int(stock),
            image_url=image_url
        )

        db.session.add(new_equipment)
        db.session.commit()
        flash(f'Peralatan {item_name} berhasil ditambahkan ke katalog!', 'success')
        return redirect(url_for('manage_equipments'))

    equipments = Equipment.query.filter_by(vendor_id=user.id).all()
    return render_template('vendor/equipments.html', equipments=equipments)


@app.route('/vendor/transactions', methods=['GET'])
@vendor_required
def vendor_transactions():
    user = User.query.get(session['user_id'])

    transactions = db.session.query(RentalTransaction, Equipment, User).\
        join(Equipment, RentalTransaction.equipment_id == Equipment.id).\
        join(User, RentalTransaction.user_id == User.id).\
        filter(Equipment.vendor_id == user.id).all()

    return render_template('vendor/transactions.html', transactions=transactions)


@app.route('/vendor/transactions/update/<int:tx_id>', methods=['POST'])
@vendor_required
def update_rental_status(tx_id):
    user = User.query.get(session['user_id'])
    new_status = request.form.get('status')

    tx = RentalTransaction.query.get_or_404(tx_id)
    eq = Equipment.query.get(tx.equipment_id)

    if eq.vendor_id != user.id:
        flash('Akses ditolak: Transaksi ini bukan milik toko Anda.', 'danger')
        return redirect(url_for('vendor_transactions'))

    if tx.status == 'active' and new_status == 'completed':
        eq.stock += tx.qty

    elif tx.status == 'pending' and new_status == 'cancelled':
        eq.stock += tx.qty

    tx.status = new_status
    db.session.commit()

    flash('Status penyewaan berhasil diperbarui!', 'success')
    return redirect(url_for('vendor_transactions'))


@app.route('/api/auth/google', methods=['POST'])
def api_google_login():
    """
    Login/Register via Google
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [email, google_id]
          properties:
            email: {type: string, example: "pendaki@gmail.com"}
            name: {type: string, example: "Aditya"}
            google_id: {type: string, example: "1092837465"}
    responses:
      200:
        description: Berhasil login dan mendapatkan Token JWT
    """
    data = request.get_json()
    token = data.get('token')

    if not token:
        return jsonify({"message": "Token tidak ditemukan"}), 400

    try:
        idinfo = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            os.getenv('GOOGLE_CLIENT_ID')
        )

        email = idinfo['email']
        google_id = idinfo['sub']
        name = idinfo.get('name', 'Pendaki')

        user = User.query.filter_by(provider_id=google_id).first()
        if not user:
            user = User(name=name, email=email, provider_id=google_id, role='pendaki', status='active')
            db.session.add(user)
            db.session.commit()

        session['user_id'] = user.id
        session['user_name'] = user.name
        session['user_role'] = user.role

        jwt_token = create_access_token(identity=str(user.id))

        return jsonify({
            'token': jwt_token,
            'user': serialize_user(user)
        }), 200

    except Exception as e:
        print(f"Error Verifikasi: {e}")
        return jsonify({"message": "Token tidak valid"}), 401


@app.route('/api/auth/register', methods=['POST'])
def api_register():
    """
    Register User Baru (Manual dengan Role)
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [name, email, password]
          properties:
            name: {type: string, example: "Pendaki Pemula"}
            email: {type: string, example: "pendaki@gmail.com"}
            password: {type: string, example: "rahasia123"}
            role: {type: string, example: "pendaki", description: "Opsi: pendaki, vendor, basecamp_admin"}
    responses:
      201:
        description: User berhasil didaftarkan
      400:
        description: Email sudah terdaftar atau data tidak lengkap
    """
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    role = data.get('role', 'pendaki')

    if not name or not email or not password:
        return jsonify({"message": "Data pendaftaran tidak lengkap"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"message": "Email sudah terdaftar!"}), 400

    otp = str(random.randint(100000, 999999))
    expiry_time = datetime.now() + timedelta(minutes=10)

    new_user = User(
        name=name,
        email=email,
        password_hash=generate_password_hash(password),
        role=role,
        status='unverified',
        otp_code=otp,
        otp_expiry=expiry_time
    )
    db.session.add(new_user)
    db.session.commit()

    try:
        msg = Message("Kode Verifikasi Summit Guide", recipients=[email])
        msg.body = f"Halo {name},\n\nKode verifikasi Anda adalah: {otp}\nKode ini berlaku selama 10 menit.\n\nTerima kasih."
        mail.send(msg)
    except Exception as e:
        print(f"Error kirim email: {e}")
        return jsonify({"message": "User terdaftar, tapi gagal mengirim email OTP."}), 500

    return jsonify({"message": "Registrasi berhasil. Silakan cek email untuk kode OTP.", "email": email}), 201


@app.route('/api/auth/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json()
    email = data.get('email')
    otp_input = data.get('otp')

    user = User.query.filter_by(email=email).first()

    if not user:
        return jsonify({"message": "User tidak ditemukan"}), 404

    if user.otp_code != otp_input:
        return jsonify({"message": "Kode OTP salah!"}), 400

    if datetime.now() > user.otp_expiry:
        return jsonify({"message": "Kode OTP sudah kedaluwarsa. Silakan minta ulang."}), 400

    user.status = 'active' if user.role == 'pendaki' else 'pending'

    user.otp_code = None
    user.otp_expiry = None
    db.session.commit()

    token = create_access_token(identity=str(user.id))

    return jsonify({
        "message": "Verifikasi berhasil!",
        "token": token,
        "user": serialize_user(user)
    }), 200


@app.route('/api/auth/login', methods=['POST'])
def api_login():
    """
    Login Biasa (Email & Password)
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required: [email, password]
          properties:
            email: {type: string, example: "pendaki@gmail.com"}
            password: {type: string, example: "rahasia123"}
    responses:
      200:
        description: Berhasil login dan mendapatkan Token JWT
      401:
        description: Email atau Password salah
      403:
        description: Akun pending atau suspended
    """
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"message": "Email dan password wajib diisi"}), 400

    user = User.query.filter_by(email=email).first()

    if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
        return jsonify({"message": "Email atau password salah!"}), 401

    if user.status == 'pending':
        return jsonify({"message": "Akun Anda masih menunggu verifikasi admin."}), 403
    if user.status == 'suspended':
        return jsonify({"message": "Akun Anda diblokir oleh sistem."}), 403

    token = create_access_token(identity=str(user.id))

    return jsonify({
        'token': token,
        'user': serialize_user(user)
    }), 200


@app.route('/api/basecamp/verify-ticket', methods=['POST'])
def api_verify_ticket():
    data = request.get_json()
    qr_code_input = data.get('qr_code')

    if not qr_code_input:
        return jsonify({"message": "Data QR Code kosong"}), 400

    ticket = Ticket.query.filter_by(qr_code=qr_code_input).first()

    if not ticket:
        return jsonify({"message": "Tiket tidak terdaftar atau tidak valid!"}), 404

    if 'user_id' in session:
        admin_user = User.query.get(session['user_id'])
        if ticket.basecamp_id != admin_user.basecamp_id:
            return jsonify({"message": "Tiket ini ditujukan untuk basecamp/jalur lain!"}), 403

    if ticket.status == 'checked_in':
        return jsonify({"message": "Tiket ini sudah pernah digunakan untuk check-in sebelumnya."}), 400

    if ticket.payment_status == 'pending':
        return jsonify({"message": "Pendaki belum melunasi pembayaran tiket simaksi."}), 400

    try:
        ticket.status = 'checked_in'
        db.session.commit()

        hiker = User.query.get(ticket.user_id)
        return jsonify({
            "message": "Check-in sukses!",
            "data": {
                "nama_pendaki": hiker.name if hiker else "Pendaki Misterius",
                "tanggal_mendaki": str(ticket.booking_date)
            }
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": "Gagal memperbarui status tiket", "error": str(e)}), 500


@app.route('/api/user/profile', methods=['GET'])
@jwt_required()
def api_get_profile():
    """
    Ambil Data Profil Mobile
    ---
    tags:
      - User Profile
    security:
      - Bearer: []
    responses:
      200:
        description: Data profil user
      404:
        description: User tidak ditemukan
    """
    current_user_id = get_jwt_identity()
    user = db.session.get(User, int(current_user_id))
    if not user:
        return jsonify({"message": "User tidak ditemukan"}), 404

    return jsonify({"user": serialize_user(user)}), 200


@app.route('/api/user/profile', methods=['PUT'])
@jwt_required()
def api_update_profile():
    """
    Update Profil Mobile (Nama, Telepon, Foto, Password)
    Mendukung 2 format:
    - application/json  -> kalau tidak upload foto
    - multipart/form-data -> kalau menyertakan foto profil
    ---
    tags:
      - User Profile
    security:
      - Bearer: []
    responses:
      200:
        description: Profil berhasil diperbarui
      400:
        description: Password lama salah / tidak diisi saat ganti password
      404:
        description: User tidak ditemukan
    """
    current_user_id = get_jwt_identity()
    user = db.session.get(User, int(current_user_id))
    if not user:
        return jsonify({"message": "User tidak ditemukan"}), 404

    is_multipart = request.content_type and 'multipart/form-data' in request.content_type

    if is_multipart:
        payload = request.form
        photo = request.files.get('profile_photo')
    else:
        payload = request.get_json(silent=True) or {}
        photo = None

    name = payload.get('name')
    if name:
        user.name = name

    phone = payload.get('phone')
    if phone:
        user.phone = phone

    # [DIUBAH] Upload foto profil mobile ke Supabase Storage,
    # bukan disimpan ke disk lokal (photo.save()) yang tidak
    # persist di Vercel.
    if photo and photo.filename != '':
        uploaded_url = upload_file_to_supabase(photo, folder="profiles")
        if uploaded_url:
            user.profile_photo = uploaded_url

    old_password = payload.get('old_password')
    new_password = payload.get('new_password')

    if new_password:
        if not old_password:
            return jsonify({"message": "Password lama wajib diisi untuk mengganti password"}), 400
        if not user.password_hash or not check_password_hash(user.password_hash, old_password):
            return jsonify({"message": "Password lama salah"}), 400
        user.password_hash = generate_password_hash(new_password)

    db.session.commit()

    return jsonify({
        "message": "Profil berhasil diperbarui",
        "user": {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "profile_photo": user.profile_photo
        }
    }), 200
    
    
    
# ==========================================
# ENDPOINT UNTUK APLIKASI MOBILE (Flutter)
# ==========================================
@app.route('/api/weather/forecast', methods=['GET'])
def api_weather_forecast():
    mountain_name = request.args.get('name', '')
    if not mountain_name:
        return jsonify({"message": "Parameter 'name' diperlukan"}), 400
    
    from weather_service import get_forecast_by_name
    data = get_forecast_by_name(mountain_name)
    if not data:
        return jsonify({"message": "Data forecast belum tersedia untuk gunung ini"}), 404
    return jsonify(data), 200


@app.route('/api/weather/history', methods=['GET'])
def api_weather_history():
    mountain_name = request.args.get('name', '')
    if not mountain_name:
        return jsonify({"message": "Parameter 'name' diperlukan"}), 400
    
    from weather_service import get_history_by_name
    data = get_history_by_name(mountain_name)
    if not data:
        return jsonify({"message": "Data histori belum tersedia untuk gunung ini"}), 404
    return jsonify(data), 200



# ==========================================================
# 2. MOUNTAIN, MAP & TICKETING
# ==========================================================

@app.route('/api/mountains', methods=['GET'])
@jwt_required()
def api_get_mountains():
    """
    Ambil Daftar Gunung & Basecamp (Nested)
    ---
    tags:
      - Mountain & Maps
    security:
      - Bearer: []
    responses:
      200:
        description: Menampilkan data koordinat, GPX gunung, dan list basecamp
    """
    mountains = Mountain.query.all()
    result = []
    for m in mountains:
        basecamps_data = [{"id": b.id, "name": b.name, "daily_quota": b.daily_quota, "ticket_price": b.ticket_price} for b in m.basecamps]
        result.append({
            'id': m.id,
            'name': m.name,
            'latitude': float(m.location_lat) if m.location_lat else None,
            'longitude': float(m.location_long) if m.location_long else None,
            'gpx_url': m.gpx_data,
            'description': m.description,
            'basecamps': basecamps_data
        })
    return jsonify(result), 200


@app.route('/api/tickets/available', methods=['GET'])
@jwt_required()
def api_get_available_tickets():
    available_basecamps = Basecamp.query.filter(Basecamp.daily_quota > 0).all()

    result = [{
        "id": b.id,
        "name": b.name,
        "mountain": b.mountain.name,
        "quota": b.daily_quota,
        "price": b.ticket_price
    } for b in available_basecamps]

    return jsonify(result), 200


@app.route('/api/tickets/book', methods=['POST'])
@jwt_required()
def api_book_ticket():
    """
    Pemesanan Tiket Terpusat
    ---
    tags:
      - Ticketing
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            basecamp_id: {type: integer, example: 1}
            hiking_date: {type: string, example: "2026-08-17"}
            quantity: {type: integer, example: 2}
    responses:
      201:
        description: Tiket berhasil dibuat
      400:
        description: Kuota tidak cukup / input tidak valid
    """
    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)
    data = request.get_json()

    basecamp_id = data.get('basecamp_id')
    hiking_date = data.get('hiking_date')
    quantity = data.get('quantity', 1)

    if not basecamp_id or not hiking_date:
        return jsonify({"message": "basecamp_id dan hiking_date wajib diisi"}), 400

    if not isinstance(quantity, int) or quantity < 1:
        return jsonify({"message": "Jumlah tiket tidak valid"}), 400

    basecamp = Basecamp.query.get(basecamp_id)
    if not basecamp:
        return jsonify({"message": "Basecamp tidak ditemukan"}), 404

    booked_tickets = Ticket.query.filter(
        Ticket.basecamp_id == basecamp_id,
        Ticket.booking_date == hiking_date,
        Ticket.status != 'cancelled'
    ).all()

    total_sudah_dipesan = sum(getattr(t, 'quantity', 1) or 1 for t in booked_tickets)

    sisa_kuota = basecamp.daily_quota - total_sudah_dipesan
    if quantity > sisa_kuota:
        return jsonify({
            "message": f"Kuota tidak cukup untuk tanggal {hiking_date}. Sisa kuota: {sisa_kuota}"
        }), 400

    unique_qr = f"TICKET-{uuid.uuid4().hex[:8].upper()}"
    total_price = int(basecamp.ticket_price) * quantity

    new_ticket = Ticket(
        user_id=int(current_user_id),
        basecamp_id=basecamp_id,
        booking_date=hiking_date,
        status='booked',
        payment_status='pending',
        qr_code=unique_qr,
        ticket_price=total_price,
        quantity=quantity,
    )

    try:
        db.session.add(new_ticket)
        db.session.commit()

        param = {
            "transaction_details": {
                "order_id": unique_qr,
                "gross_amount": total_price
            },
            "customer_details": {
                "first_name": user.name,
                "email": user.email,
                "phone": user.phone or "08123456789"
            },
            "item_details": [{
                "id": f"BC-{basecamp.id}",
                "price": int(basecamp.ticket_price),
                "quantity": quantity,
                "name": f"Simaksi {basecamp.name}"
            }]
        }

        transaction = snap.create_transaction(param)
        snap_token = transaction['token']
        redirect_url = transaction['redirect_url']

        return jsonify({
            "message": "Tiket berhasil dipesan",
            "ticket_id": new_ticket.id,
            "qr_code": unique_qr,
            "quantity": quantity,
            "total_price": total_price,
            "snap_token": snap_token,
            "payment_url": redirect_url
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"message": "Gagal membuat tiket", "error": str(e)}), 500


@app.route('/api/tickets/my', methods=['GET'])
@jwt_required()
def api_my_tickets():
    current_user_id = get_jwt_identity()
    tickets = Ticket.query.filter_by(user_id=int(current_user_id)).order_by(Ticket.id.desc()).all()

    result = []
    for t in tickets:
        basecamp = Basecamp.query.get(t.basecamp_id)
        mountain = Mountain.query.get(basecamp.mountain_id) if basecamp else None
        result.append({
            "id": t.id,
            "nama_gunung": mountain.name if mountain else "-",
            "basecamp": basecamp.name if basecamp else "-",
            "booking_date": t.booking_date,
            "status": t.status,
            "payment_status": t.payment_status,
            "qr_code": t.qr_code,
            "harga": t.ticket_price,
        })

    return jsonify(result), 200


# ==========================================================
# 3. RENTAL (BARANG & PORTER)
# ==========================================================

@app.route('/api/rental/catalog', methods=['GET'])
@jwt_required()
def api_get_rental():
    """
    Katalog Barang Rental Multi-Vendor
    ---
    tags:
      - Rental & Booking
    security:
      - Bearer: []
    parameters:
      - in: query
        name: basecamp_id
        type: integer
        required: false
        description: Filter katalog berdasarkan basecamp pendaki
    responses:
      200:
        description: Daftar barang tersedia
    """
    basecamp_id = request.args.get('basecamp_id')

    query = Equipment.query.filter(Equipment.stock > 0)
    if basecamp_id:
        query = query.filter_by(basecamp_id=basecamp_id)

    items = query.all()

    available_items = [{
        "id": i.id,
        "vendor_id": i.vendor_id,
        "basecamp_id": i.basecamp_id,
        "name": i.item_name,
        "price": float(i.price_per_day),
        "stock": i.stock,
        "image_url": i.image_url
    } for i in items]

    return jsonify({"items": available_items}), 200


@app.route('/api/rental/checkout', methods=['POST'])
@jwt_required()
def api_checkout():
    """
    Sewa Barang (Mencatat Transaksi)
    ---
    tags:
      - Rental & Booking
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            item_id: {type: integer, example: 2}
            qty: {type: integer, example: 1}
            start_date: {type: string, example: "2026-08-15"}
            end_date: {type: string, example: "2026-08-17"}
    responses:
      200:
        description: Transaksi berhasil dicatat dan stok dikurangi
    """
    current_user_id = get_jwt_identity()
    data = request.get_json()

    item_id = data.get('item_id')
    qty = data.get('qty', 1)
    start_date_str = data.get('start_date')
    end_date_str = data.get('end_date')

    item = Equipment.query.get(item_id)

    if not item:
        return jsonify({"message": "Barang tidak ditemukan"}), 404
    if item.stock < qty:
        return jsonify({"message": f"Stok tidak cukup. Sisa: {item.stock}"}), 400
    if not start_date_str or not end_date_str:
        return jsonify({"message": "Tanggal sewa wajib diisi"}), 400

    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        rent_days = (end_date - start_date).days
        if rent_days <= 0:
            rent_days = 1

        item.stock -= qty

        total_harga = float(item.price_per_day) * qty * rent_days

        new_transaction = RentalTransaction(
            user_id=int(current_user_id),
            equipment_id=item.id,
            qty=qty,
            start_date=start_date,
            end_date=end_date,
            total_price=total_harga,
            status='pending',
            payment_status='pending'
        )
        db.session.add(new_transaction)
        db.session.commit()

        rental_order_id = f"RENTAL-{new_transaction.id}-{uuid.uuid4().hex[:4].upper()}"

        user = User.query.get(current_user_id)

        param = {
            "transaction_details": {
                "order_id": rental_order_id,
                "gross_amount": int(total_harga)
            },
            "customer_details": {
                "first_name": user.name,
                "email": user.email,
            },
            "item_details": [{
                "id": str(item.id),
                "price": int(item.price_per_day),
                "quantity": qty * rent_days,
                "name": item.item_name
            }]
        }

        transaction = snap.create_transaction(param)

        return jsonify({
            "message": "Pemesanan rental berhasil dicatat!",
            "transaction_id": new_transaction.id,
            "order_id": rental_order_id,
            "snap_token": transaction['token'],
            "payment_url": transaction['redirect_url']
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"message": "Terjadi kesalahan server", "error": str(e)}), 500


@app.route('/api/payment/webhook', methods=['POST'])
def payment_webhook():
    """
    Endpoint ini didengarkan (listen) oleh server Midtrans.
    Dilarang memasang @jwt_required() di sini karena yang mengakses adalah server Midtrans, bukan user.
    """
    notification = request.get_json()
    if not notification:
        return jsonify({"status": "error", "message": "No data"}), 400

    order_id = notification.get('order_id')
    transaction_status = notification.get('transaction_status')
    fraud_status = notification.get('fraud_status')

    is_paid = False
    if transaction_status == 'capture':
        if fraud_status == 'challenge':
            is_paid = False
        elif fraud_status == 'accept':
            is_paid = True
    elif transaction_status == 'settlement':
        is_paid = True

    if order_id.startswith('TICKET-'):
        ticket = Ticket.query.filter_by(qr_code=order_id).first()
        if ticket:
            if is_paid:
                ticket.payment_status = 'paid'
            elif transaction_status in ['cancel', 'deny', 'expire']:
                ticket.payment_status = 'failed'
            db.session.commit()

    elif order_id.startswith('RENTAL-'):
        tx_id = int(order_id.split('-')[1])
        rental_tx = RentalTransaction.query.get(tx_id)
        if rental_tx:
            if is_paid:
                rental_tx.payment_status = 'paid'
            elif transaction_status in ['cancel', 'deny', 'expire']:
                rental_tx.payment_status = 'failed'
                eq = Equipment.query.get(rental_tx.equipment_id)
                if eq:
                    eq.stock += rental_tx.qty
            db.session.commit()

    return jsonify({"status": "ok"}), 200


@app.route('/api/user/history', methods=['GET'])
@jwt_required()
def api_history():
    """
    Riwayat Tiket Pendaki
    ---
    tags:
      - User Profile
    security:
      - Bearer: []
    responses:
      200:
        description: Menampilkan histori tiket ke basecamp
    """
    current_user_id = get_jwt_identity()
    tickets = Ticket.query.filter_by(user_id=int(current_user_id)).all()

    history_data = []
    for t in tickets:
        basecamp = Basecamp.query.get(t.basecamp_id)
        history_data.append({
            "ticket_id": t.id,
            "basecamp_name": basecamp.name if basecamp else "Unknown",
            "date": str(t.booking_date),
            "status": t.status,
            "payment_status": t.payment_status,
            "qr_code": t.qr_code
        })

    return jsonify({"history": history_data}), 200


app.register_blueprint(admin_bp)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)