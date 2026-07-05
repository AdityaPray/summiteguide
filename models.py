from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

# ==============================================================================
# 1. TABEL MASTER: GUNUNG
# ==============================================================================
class Mountain(db.Model):
    __tablename__ = 'mountains'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    location_lat = db.Column(db.Float)
    location_long = db.Column(db.Float)
    gpx_data = db.Column(db.String(255))
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_verified = db.Column(db.Boolean, default=False)

    # Relasi: Memudahkan pemanggilan (misal: mountain.basecamps)
    basecamps = db.relationship('Basecamp', backref='mountain', lazy=True, cascade="all, delete-orphan")

# ==============================================================================
# 2. TABEL MASTER: BASECAMP / JALUR PENDAKIAN
# ==============================================================================
class Basecamp(db.Model):
    __tablename__ = 'basecamps'
    id = db.Column(db.Integer, primary_key=True)
    mountain_id = db.Column(db.Integer, db.ForeignKey('mountains.id', ondelete='CASCADE'))
    name = db.Column(db.String(100), nullable=False)
    daily_quota = db.Column(db.Integer, default=100)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    ticket_price = db.Column(db.Float, default=0.0)

    # Relasi
    users = db.relationship('User', backref='basecamp', lazy=True)
    equipments = db.relationship('Equipment', backref='basecamp', lazy=True)
    tickets = db.relationship('Ticket', backref='basecamp', lazy=True)
    
    

# ==============================================================================
# 3. TABEL SENTRAL: USERS (RBAC, KYC, & Multi-Tenancy)
# ==============================================================================
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    phone = db.Column(db.String(20))
    profile_photo = db.Column(db.String(255), default='/static/uploads/default_avatar.jpg')
    
    # Autentikasi
    provider_id = db.Column(db.String(255), unique=True, nullable=True) 
    password_hash = db.Column(db.String(255), nullable=True) 
    
    # Role & Status Akses
    role = db.Column(db.String(50), default='pendaki') 
    status = db.Column(db.String(20), default='pending') 
    
    # Isolasi Data (Vendor / Basecamp Admin bertugas di mana?)
    basecamp_id = db.Column(db.Integer, db.ForeignKey('basecamps.id', ondelete='SET NULL'), nullable=True)
    shop_name = db.Column(db.String(100), nullable=True) 
    
    # Hierarki Basecamp (Self-Referencing FK untuk sistem shift)
    parent_admin_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=True)
    
    # KYC & Verifikasi Kelembagaan
    ktp_number = db.Column(db.String(16), unique=True, nullable=True)
    ktp_image = db.Column(db.String(255), nullable=True)
    bank_name = db.Column(db.String(50), nullable=True)
    bank_account = db.Column(db.String(50), nullable=True)
    official_letter = db.Column(db.String(255), nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relasi Self-Referential (Memudahkan Koordinator melihat daftar stafnya)
    staff_members = db.relationship('User', backref=db.backref('coordinator', remote_side=[id]))
    
    # Relasi lainnya
    equipments = db.relationship('Equipment', backref='vendor', lazy=True)
    tickets = db.relationship('Ticket', backref='user', lazy=True)
    rental_transactions = db.relationship('RentalTransaction', backref='user', lazy=True)
    
    #OTP
    otp_code = db.Column(db.String(6), nullable=True)
    otp_expiry = db.Column(db.DateTime, nullable=True)

# ==============================================================================
# 4. TABEL RENTAL: KATALOG ALAT
# ==============================================================================
class Equipment(db.Model):
    __tablename__ = 'equipments'
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'))
    basecamp_id = db.Column(db.Integer, db.ForeignKey('basecamps.id', ondelete='CASCADE'))
    item_name = db.Column(db.String(100), nullable=False)
    price_per_day = db.Column(db.Numeric(10, 2), nullable=False)
    stock = db.Column(db.Integer, default=0)
    image_url = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relasi
    transactions = db.relationship('RentalTransaction', backref='equipment', lazy=True)

# ==============================================================================
# 5. TABEL TRANSAKSI: TIKET PENDAKIAN (Terpusat)
# ==============================================================================
class Ticket(db.Model):
    __tablename__ = 'tickets'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'))
    basecamp_id = db.Column(db.Integer, db.ForeignKey('basecamps.id', ondelete='CASCADE'))
    booking_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(50), default='booked') 
    payment_status = db.Column(db.String(50), default='pending') 
    qr_code = db.Column(db.String(255), unique=True, nullable=True) 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    ticket_price = db.Column(db.Float, nullable=False, default=0.0)
    quantity = db.Column(db.Integer, default=1)

# ==============================================================================
# 6. TABEL TRANSAKSI: SEWA RENTAL
# ==============================================================================
class RentalTransaction(db.Model):
    __tablename__ = 'rental_transactions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'))
    equipment_id = db.Column(db.Integer, db.ForeignKey('equipments.id', ondelete='CASCADE'))
    qty = db.Column(db.Integer, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    total_price = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(50), default='pending') 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    payment_status = db.Column(db.String(50), default='pending')
    
    
