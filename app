import os
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from models import db, User, Mountain, Basecamp, Equipment, Ticket, RentalTransaction
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flasgger import Swagger
from datetime import timedelta

load_dotenv()
app = Flask(__name__)

# ==========================================
# 1. KONFIGURASI SUPABASE & KEAMANAN VIA .ENV
# ==========================================
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('SUPABASE_DATABASE_URI')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.getenv('FLASK_SECRET_KEY')
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=7)

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

db.init_app(app)
jwt = JWTManager(app)

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
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('web_dashboard'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        user = User.query.filter_by(email=email, password=password).first()
        if user:
            session['user_id'] = user.id
            session['user_name'] = user.name
            session['user_role'] = user.role
            return redirect(url_for('web_dashboard'))
        else:
            flash('Email atau password salah!', 'danger')
            return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Anda berhasil logout.', 'success')
    return redirect(url_for('login'))

@app.route('/')
def web_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    user = User.query.get(session['user_id'])
    return render_template('dashboard.html', user=user)

@app.route('/mountains')
def web_mountains():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    user = User.query.get(session['user_id'])
    if user.role != 'super_admin':
        flash('Akses ditolak! Halaman ini khusus Super Admin.', 'danger')
        return redirect(url_for('web_dashboard'))
        
    mountains = Mountain.query.all()
    return render_template('mountains.html', user=user, mountains=mountains)

@app.route('/inventory')
def web_inventory():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    user = User.query.get(session['user_id'])
    if user.role not in ['admin_vendor', 'super_admin']:
        flash('Akses ditolak! Halaman ini khusus Vendor.', 'danger')
        return redirect(url_for('web_dashboard'))
        
    rental = Rental.query.filter_by(owner_id=user.id).first()
    items = EquipmentItem.query.filter_by(rental_id=rental.id).all() if rental else []
    return render_template('inventory.html', user=user, items=items)

# ==========================================
# 3. RESTFUL API ENDPOINTS DENGAN DOKUMENTASI LENGKAP
# ==========================================

# ==========================================================
# 1. AUTHENTICATION & PROFILE
# ==========================================================

@app.route('/api/auth/google', methods=['POST'])
def api_google_login():
    """
    Login/Register via Google
    Endpoint untuk sinkronisasi akun Google ke Database.
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
    google_id = data.get('google_id')
    email = data.get('email')
    
    if not google_id or not email:
        return jsonify({"message": "Data Google tidak lengkap"}), 400

    user = User.query.filter_by(provider_id=google_id).first()

    if not user:
        user = User(name=data.get('name', 'Pendaki'), email=email, provider_id=google_id, role='pendaki')
        db.session.add(user)
        db.session.commit()
    else:
        user.name = data.get('name', user.name)
        db.session.commit()
        
    # GANTI MENJADI INI:
    token = create_access_token(identity=str(user.id))
    return jsonify({'token': token, 'user': {'id': user.id, 'name': user.name, 'email': user.email, 'role': user.role}}), 200

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    """
    Register User Baru (Manual)
    Endpoint untuk pendaftaran akun menggunakan Email & Password.
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

    if not name or not email or not password:
        return jsonify({"message": "Data pendaftaran tidak lengkap"}), 400

    # Cek apakah email sudah terdaftar sebelumnya
    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({"message": "Email sudah terdaftar!"}), 400

    # Buat user baru dengan password yang dienkripsi (hash)
    new_user = User(
        name=name,
        email=email,
        password=generate_password_hash(password),
        role='pendaki'
    )
    db.session.add(new_user)
    db.session.commit()

    return jsonify({"message": "Registrasi berhasil, silakan login"}), 201


@app.route('/api/auth/login', methods=['POST'])
def api_login():
    """
    Login Biasa (Email & Password)
    Endpoint untuk login manual dan mendapatkan token JWT.
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
    """
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"message": "Email dan password wajib diisi"}), 400

    # Cari user di database
    user = User.query.filter_by(email=email).first()

    # Validasi user ada, memiliki password (bukan akun eksklusif google), dan passwordnya cocok
    if not user or not user.password or not check_password_hash(user.password, password):
        return jsonify({"message": "Email atau password salah!"}), 401

    # Jika benar, berikan token JWT
    # GANTI MENJADI INI:
    token = create_access_token(identity=str(user.id))
    
    return jsonify({
        'token': token, 
        'user': {'id': user.id, 'name': user.name, 'email': user.email, 'role': user.role}
    }), 200

@app.route('/api/user/profile', methods=['PUT'])
@jwt_required()
def api_update_profile():
    """
    Update Profil & Password
    Mendukung enkripsi (hashing) password otomatis.
    ---
    tags:
      - User Profile
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            name: {type: string}
            password: {type: string}
    responses:
      200:
        description: Profil berhasil diperbarui
    """
    current_user = get_jwt_identity()
    # GANTI MENJADI INI:
    current_user_id = get_jwt_identity()
    user = User.query.get(int(current_user_id))
    if not user: return jsonify({"message": "User tidak ditemukan"}), 404

    data = request.get_json()
    if 'name' in data: user.name = data['name']
    if 'password' in data: user.password = generate_password_hash(data['password']) 
        
    db.session.commit()
    return jsonify({"message": "Profil berhasil diperbarui", "new_name": user.name}), 200

# ==========================================================
# 2. MOUNTAIN, MAP & TICKETING
# ==========================================================

@app.route('/api/mountains', methods=['GET'])
@jwt_required()
def api_get_mountains():
    """
    Ambil Daftar Gunung & Peta
    ---
    tags:
      - Mountain & Maps
    security:
      - Bearer: []
    responses:
      200:
        description: Menampilkan data koordinat dan GPX gunung
    """
    mountains = Mountain.query.all()
    return jsonify([{
        'id': m.id, 
        'name': m.name, 
        'latitude': float(m.location_lat) if m.location_lat else None,
        'longitude': float(m.location_long) if m.location_long else None,
        'gpx_url': m.gpx_data,
        'description': m.description
    } for m in mountains]), 200

@app.route('/api/tickets/book', methods=['POST'])
@jwt_required()
def api_book_ticket():
    """
    Pemesanan Tiket Pendakian
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
            mountain_id: {type: integer, example: 1}
            hiking_date: {type: string, example: "2026-08-17"}
    responses:
      201:
        description: Tiket berhasil dibuat
    """
    current_user_id = get_jwt_identity()
    data = request.get_json()
    
    if not data.get('mountain_id') or not data.get('hiking_date'):
        return jsonify({"message": "Data pemesanan tidak lengkap"}), 400
        
    new_ticket = Ticket(user_id=int(current_user_id), mountain_id=data.get('mountain_id'), booking_date=data.get('hiking_date'), status='booked')
    db.session.add(new_ticket)
    db.session.commit()
    return jsonify({"message": "Tiket berhasil dipesan", "ticket_id": new_ticket.id}), 201

# ==========================================================
# 3. RENTAL (BARANG & PORTER)
# ==========================================================

@app.route('/api/rental/catalog', methods=['GET'])
@jwt_required()
def api_get_rental():
    """
    Katalog Barang Rental
    Hanya menampilkan barang yang stoknya > 0
    ---
    tags:
      - Rental & Booking
    security:
      - Bearer: []
    responses:
      200:
        description: Daftar barang tersedia
    """
    items = EquipmentItem.query.all()
    available_items = [{"id": i.id, "name": i.item_name, "price": i.price_per_day, "stock": i.stock} for i in items if hasattr(i, 'stock') and i.stock > 0]
    return jsonify({"items": available_items}), 200

@app.route('/api/rental/checkout', methods=['POST'])
@jwt_required()
def api_checkout():
    """
    Sewa Barang (Atomic Transaction)
    Otomatis mengurangi stok dan mencegah stok minus.
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
    responses:
      200:
        description: Stok berhasil diperbarui
    """
    data = request.get_json()
    item_id = data.get('item_id')
    qty = data.get('qty', 1)
    
    item = EquipmentItem.query.get(item_id)
    if not item: return jsonify({"message": "Barang tidak ditemukan"}), 404
    if item.stock < qty: return jsonify({"message": f"Stok tidak cukup. Sisa: {item.stock}"}), 400
        
    try:
        item.stock -= qty
        db.session.commit()
        return jsonify({"message": "Pemesanan berhasil, stok diperbarui!"}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": "Terjadi kesalahan server", "error": str(e)}), 500

@app.route('/api/user/history', methods=['GET'])
@jwt_required()
def api_history():
    """
    Riwayat Pendakian User
    ---
    tags:
      - User Profile
    security:
      - Bearer: []
    responses:
      200:
        description: Menampilkan histori tiket
    """
    current_user = get_jwt_identity()
    tickets = Ticket.query.filter_by(user_id=current_user['id']).all()
    history_data = [{"ticket_id": t.id, "mountain_id": t.mountain_id, "date": str(t.booking_date), "status": t.status} for t in tickets]
    return jsonify({"history": history_data}), 200

# ==========================================================
# 4. AI SCAN (STATELESS)
# ==========================================================

@app.route('/api/ai/scan', methods=['POST'])
@jwt_required()
def api_ai_scan():
    """
    Validasi Alat (YOLOv8 Inference)
    Upload gambar dari kamera Flutter untuk dianalisis oleh AI.
    ---
    tags:
      - AI Features
    security:
      - Bearer: []
    consumes:
      - multipart/form-data
    parameters:
      - in: formData
        name: image
        type: file
        required: true
        description: File gambar perlengkapan pendakian
    responses:
      200:
        description: Hasil deteksi YOLOv8
    """
    if 'image' not in request.files: return jsonify({"message": "Tidak ada file gambar!"}), 400
    file = request.files['image']
    if file.filename == '': return jsonify({"message": "File kosong!"}), 400

    # Simulasi hasil deteksi AI
    detected_items = ["Tenda", "Carrier", "Sepatu Gunung"] 
    return jsonify({"status": "Success", "message": "Gambar berhasil dianalisis", "detected_items": detected_items, "is_complete": len(detected_items) >= 3}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)