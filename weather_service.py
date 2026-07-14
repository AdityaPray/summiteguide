"""
weather_service.py
===================
Modul untuk mengambil data cuaca (perkiraan & histori) secara OTOMATIS
HANYA berdasarkan NAMA GUNUNG (tidak lagi bergantung pada kolom
location_lat/location_long di Postgres/Supabase, karena data itu masih asal).

Koordinat di-resolve otomatis via Open-Meteo Geocoding API (gratis, tanpa key)
berdasarkan nama gunung, lalu hasilnya di-cache di MongoDB (collection
`mountain_coordinates`) supaya tidak perlu geocoding berulang setiap kali
scheduler jalan.

Sumber data:
- Koordinat                    -> Open-Meteo Geocoding API (gratis, tanpa key)
- Forecast (perkiraan 5 hari)  -> OpenWeatherMap  (butuh API key)
- History (histori cuaca)      -> Open-Meteo Archive API (gratis, tanpa key)

[BARU] Riwayat/log setiap kali update dijalankan (kapan, berapa gunung
berhasil/gagal, berapa lama) dicatat di collection TERPISAH
`weather_update_logs`, supaya tidak tercampur dengan data cuaca aktual
di `weather_forecast` / `weather_history` / `mountain_coordinates`.

Cara pakai:
    from weather_service import update_all_mountains_weather, get_forecast_by_name, get_history_by_name

    # Dipanggil terjadwal (APScheduler) atau manual via endpoint admin
    summary = update_all_mountains_weather(["Gunung Slamet", "Gunung Merbabu", ...])

    # Dipanggil dari endpoint API mobile
    get_forecast_by_name("Gunung Slamet")
    get_history_by_name("Gunung Slamet")

    # Dipanggil untuk lihat riwayat update terakhir (mis. untuk monitoring)
    get_update_logs(limit=20)
"""

import os
import requests
from datetime import datetime, timedelta
from pymongo import MongoClient
from dotenv import load_dotenv

# Load .env di sini juga (jangan hanya mengandalkan load_dotenv() di app.py),
# supaya MONGO_URI selalu terbaca berapa pun urutan import-nya.
load_dotenv()

# ==============================================================================
# KONFIGURASI (ambil dari .env, JANGAN hardcode di production)
# ==============================================================================
MONGO_URI = os.getenv("MONGO_URI")  # contoh: mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/?appName=Cluster0
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

if not MONGO_URI:
    print("[WEATHER][WARNING] MONGO_URI tidak ditemukan di .env! Pymongo akan gagal connect ke Atlas.")
if not OPENWEATHER_API_KEY:
    print("[WEATHER][WARNING] OPENWEATHER_API_KEY tidak ditemukan di .env! Forecast tidak akan berfungsi.")


def normalize_name(mountain_name: str) -> str:
    """
    Normalisasi nama gunung supaya konsisten di seluruh sistem (selalu huruf
    kecil, tanpa spasi berlebih). Ini WAJIB dipakai di SEMUA tempat yang
    baca/tulis mountain_name — baik saat cron menulis data dari nama Postgres
    (mis. "Gunung Slamet"), maupun saat endpoint mobile membaca dengan nama
    yang mungkin beda kapitalisasi (mis. "gunung slamet" dari URL).
    Tanpa ini, MongoDB akan menganggap "Gunung Slamet" dan "gunung slamet"
    sebagai 2 data yang berbeda (case-sensitive), menyebabkan 404 padahal
    datanya sebenarnya ada.
    """
    return mountain_name.strip().lower()


# ==============================================================================
# TABEL KONVERSI KODE CUACA WMO -> TEKS (dipakai untuk data HISTORY)
# ==============================================================================
WMO_WEATHER_CODES = {
    0:  ("Cerah", "clear"),
    1:  ("Cerah Berawan", "partly_cloudy"),
    2:  ("Berawan Sebagian", "partly_cloudy"),
    3:  ("Berawan Tebal", "cloudy"),
    45: ("Berkabut", "fog"),
    48: ("Kabut Beku", "fog"),
    51: ("Gerimis Ringan", "drizzle"),
    53: ("Gerimis Sedang", "drizzle"),
    55: ("Gerimis Lebat", "drizzle"),
    56: ("Gerimis Beku Ringan", "drizzle"),
    57: ("Gerimis Beku Lebat", "drizzle"),
    61: ("Hujan Ringan", "rain"),
    63: ("Hujan Sedang", "rain"),
    65: ("Hujan Lebat", "rain"),
    66: ("Hujan Beku Ringan", "rain"),
    67: ("Hujan Beku Lebat", "rain"),
    71: ("Salju Ringan", "snow"),
    73: ("Salju Sedang", "snow"),
    75: ("Salju Lebat", "snow"),
    77: ("Butiran Salju", "snow"),
    80: ("Hujan Lokal Ringan", "rain"),
    81: ("Hujan Lokal Sedang", "rain"),
    82: ("Hujan Lokal Lebat", "rain"),
    85: ("Hujan Salju Ringan", "snow"),
    86: ("Hujan Salju Lebat", "snow"),
    95: ("Badai Petir", "thunderstorm"),
    96: ("Badai Petir + Hujan Es Ringan", "thunderstorm"),
    99: ("Badai Petir + Hujan Es Lebat", "thunderstorm"),
}


def weathercode_to_text(code: int):
    """Konversi kode WMO jadi (teks_indonesia, kategori_icon). Default 'Tidak diketahui' kalau kode asing."""
    return WMO_WEATHER_CODES.get(code, ("Tidak diketahui", "unknown"))


GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
HISTORY_URL = "https://archive-api.open-meteo.com/v1/archive"

_mongo_client = None
_weather_db = None


def get_weather_db():
    """Lazy init koneksi Mongo supaya tidak connect saat modul di-import (mis. untuk testing)."""
    global _mongo_client, _weather_db
    if _weather_db is None:
        _mongo_client = MongoClient(MONGO_URI)
        _weather_db = _mongo_client["summit_guide_weather"]
    return _weather_db


# ==============================================================================
# 0. RESOLVE KOORDINAT DARI NAMA GUNUNG (manual list -> cache -> geocoding fallback)
# ==============================================================================
KNOWN_MOUNTAIN_COORDS = {
    "gunung slamet": (-7.242, 109.208),
    "gunung merbabu": (-7.452, 110.438),
    "gunung sumbing": (-7.384, 110.070),
    "gunung sindoro": (-7.301, 109.998),
    "gunung prau": (-7.185, 109.923),
    "gunung merapi": (-7.540, 110.446),
    "gunung lawu": (-7.625, 111.193),
    "gunung ungaran": (-7.181, 110.336),
    "gunung muria": (-6.616, 110.885),
    "gunung andong": (-7.386, 110.363),
}


def _lookup_known_coords(mountain_name: str):
    """Cocokkan ke daftar manual, abaikan besar/kecil huruf & keterangan dalam kurung
    (mis. 'Gunung Merapi (Jateng-DIY)' -> 'gunung merapi')."""
    clean = mountain_name.lower().split("(")[0].strip()
    return KNOWN_MOUNTAIN_COORDS.get(clean)


def resolve_mountain_coordinates(mountain_name: str):
    """
    Cari lat/lon dari nama gunung, urutan prioritas:
    1. Cache di Mongo (mountain_coordinates) - kalau sudah pernah di-resolve
    2. Daftar manual known-good (KNOWN_MOUNTAIN_COORDS) - paling akurat
    3. Geocoding Open-Meteo - fallback terakhir untuk gunung yang belum dikenal
    """
    mountain_name = normalize_name(mountain_name)
    coll = get_weather_db()["mountain_coordinates"]

    cached = coll.find_one({"mountain_name": mountain_name})
    if cached and cached.get("lat") is not None:
        return cached["lat"], cached["lon"]

    manual = _lookup_known_coords(mountain_name)
    if manual:
        lat, lon = manual
        coll.update_one(
            {"mountain_name": mountain_name},
            {"$set": {
                "mountain_name": mountain_name,
                "lat": lat, "lon": lon,
                "source": "manual-known-list",
                "resolved_at": datetime.utcnow(),
            }},
            upsert=True,
        )
        print(f"[WEATHER][MANUAL] '{mountain_name}' -> ({lat}, {lon})")
        return lat, lon

    clean_name = mountain_name.replace("gunung", "").split("(")[0].strip()

    params = {"name": clean_name, "count": 5, "language": "id", "format": "json"}
    try:
        resp = requests.get(GEOCODE_URL, params=params, timeout=15)
    except requests.RequestException as e:
        print(f"[WEATHER][GEOCODE] Gagal koneksi untuk {mountain_name}: {e}")
        return None, None

    if resp.status_code != 200:
        print(f"[WEATHER][GEOCODE] Gagal geocode {mountain_name}: {resp.status_code}")
        return None, None

    results = resp.json().get("results", [])
    if not results:
        print(f"[WEATHER][GEOCODE] Tidak ditemukan koordinat untuk '{mountain_name}' (coba nama lain)")
        return None, None

    best = next((r for r in results if r.get("country_code") == "ID"), results[0])
    lat, lon = best["latitude"], best["longitude"]

    coll.update_one(
        {"mountain_name": mountain_name},
        {
            "$set": {
                "mountain_name": mountain_name,
                "lat": lat,
                "lon": lon,
                "matched_place": best.get("name"),
                "admin_area": best.get("admin1"),
                "source": "open-meteo-geocoding",
                "resolved_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )
    print(f"[WEATHER][GEOCODE] '{mountain_name}' -> {best.get('name')} ({lat}, {lon})")
    return lat, lon


# ==============================================================================
# 1. FORECAST (Perkiraan Cuaca) - OpenWeatherMap
# ==============================================================================
def update_forecast_for_mountain(mountain_name: str) -> bool:
    """Cukup modal nama gunung — koordinat di-resolve otomatis via geocoding."""
    mountain_name = normalize_name(mountain_name)
    lat, lon = resolve_mountain_coordinates(mountain_name)
    if lat is None:
        return False

    params = {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric"}

    try:
        resp = requests.get(FORECAST_URL, params=params, timeout=15)
    except requests.RequestException as e:
        print(f"[WEATHER][FORECAST] Gagal koneksi untuk {mountain_name}: {e}")
        return False

    if resp.status_code != 200:
        print(f"[WEATHER][FORECAST] Gagal ambil data {mountain_name}: {resp.status_code}")
        return False

    data = resp.json()

    OWM_MAIN_TO_ICON = {
        "Clear": "clear",
        "Clouds": "cloudy",
        "Rain": "rain",
        "Drizzle": "drizzle",
        "Thunderstorm": "thunderstorm",
        "Snow": "snow",
        "Mist": "fog", "Fog": "fog", "Haze": "fog", "Smoke": "fog",
    }

    forecast_list = [
        {
            "datetime": item["dt_txt"],
            "temp": item["main"]["temp"],
            "humidity": item["main"]["humidity"],
            "condition": item["weather"][0]["description"].capitalize(),
            "icon_category": OWM_MAIN_TO_ICON.get(item["weather"][0]["main"], "unknown"),
            "wind_speed": item["wind"]["speed"],
        }
        for item in data.get("list", [])
    ]

    get_weather_db()["weather_forecast"].update_one(
        {"mountain_name": mountain_name},
        {
            "$set": {
                "mountain_name": mountain_name,
                "lat": lat,
                "lon": lon,
                "forecast": forecast_list,
                "updated_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )
    print(f"[WEATHER][FORECAST] OK: {mountain_name}")
    return True


# ==============================================================================
# 2. HISTORY (Histori Cuaca) - Open-Meteo Archive (tanpa API key)
# ==============================================================================
def update_history_for_mountain(mountain_name: str, days_back: int = 365) -> bool:
    """Cukup modal nama gunung — koordinat di-resolve otomatis via geocoding."""
    mountain_name = normalize_name(mountain_name)
    lat, lon = resolve_mountain_coordinates(mountain_name)
    if lat is None:
        return False

    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days_back)

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": "temperature_2m,relative_humidity_2m,weathercode,windspeed_10m",
        "timezone": "Asia/Jakarta",
    }

    try:
        resp = requests.get(HISTORY_URL, params=params, timeout=30)
    except requests.RequestException as e:
        print(f"[WEATHER][HISTORY] Gagal koneksi untuk {mountain_name}: {e}")
        return False

    if resp.status_code != 200:
        print(f"[WEATHER][HISTORY] Gagal ambil data {mountain_name}: {resp.status_code}")
        return False

    data = resp.json()
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    history_records = []
    for i in range(len(times)):
        code = hourly["weathercode"][i]
        condition_text, icon_category = weathercode_to_text(code)
        history_records.append({
            "datetime": times[i],
            "temp": hourly["temperature_2m"][i],
            "humidity": hourly["relative_humidity_2m"][i],
            "condition": condition_text,
            "icon_category": icon_category,
            "wind_speed": hourly["windspeed_10m"][i],
        })

    get_weather_db()["weather_history"].update_one(
        {"mountain_name": mountain_name},
        {
            "$set": {
                "mountain_name": mountain_name,
                "lat": lat,
                "lon": lon,
                "history": history_records,
                "updated_at": datetime.utcnow(),
            }
        },
        upsert=True,
    )
    print(f"[WEATHER][HISTORY] OK: {mountain_name} ({len(history_records)} jam data)")
    return True


# ==============================================================================
# 3. LOGGING RIWAYAT UPDATE (collection TERPISAH: weather_update_logs)
# ==============================================================================
# Sengaja dipisah dari weather_forecast/weather_history/mountain_coordinates
# supaya data cuaca aktual tidak tercampur dengan data "meta" (kapan cron
# jalan, berapa gunung berhasil/gagal, berapa lama prosesnya). Ini juga
# memudahkan kalau nanti mau kasih TTL index khusus di log (misal auto-hapus
# log yang lebih tua dari 90 hari) tanpa memengaruhi data cuaca.
def log_update_run(summary: dict):
    """Simpan satu entri log ke collection weather_update_logs."""
    try:
        coll = get_weather_db()["weather_update_logs"]
        coll.insert_one({**summary, "logged_at": datetime.utcnow()})
    except Exception as e:
        # Kegagalan mencatat log TIDAK BOLEH menggagalkan proses update cuaca itu
        # sendiri — cukup print supaya kelihatan di runtime logs Vercel/lokal.
        print(f"[WEATHER][LOG] Gagal mencatat log update: {e}")


def get_update_logs(limit: int = 20):
    """
    Ambil riwayat update terakhir, terbaru dulu. Berguna untuk endpoint
    monitoring/admin (mis. menampilkan status cron terakhir di dashboard).
    """
    coll = get_weather_db()["weather_update_logs"]
    cursor = coll.find({}, {"_id": 0}).sort("started_at", -1).limit(limit)
    return list(cursor)


# ==============================================================================
# 4. FUNGSI GABUNGAN - dipanggil oleh scheduler
# ==============================================================================
def update_all_mountains_weather(mountain_names: list) -> dict:
    """
    mountain_names: list of string, contoh:
        [m.name for m in Mountain.query.all()]
    Tidak perlu lat/lon lagi — semua di-resolve otomatis dari nama.

    Sekarang mengembalikan ringkasan (summary) hasil update, dan mencatatnya
    ke collection weather_update_logs. Kalau ada error tak terduga di tengah
    proses, tetap dicatat sebagai status 'error' sebelum di-raise ulang,
    supaya kegagalan tidak jadi "senyap" tanpa jejak di Mongo.
    """
    started_at = datetime.utcnow()
    details = []

    try:
        for name in mountain_names:
            forecast_ok = update_forecast_for_mountain(name)
            history_ok = update_history_for_mountain(name)
            details.append({
                "mountain_name": normalize_name(name),
                "forecast_ok": forecast_ok,
                "history_ok": history_ok,
            })
    except Exception as e:
        finished_at = datetime.utcnow()
        summary = {
            "status": "error",
            "error_message": str(e),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": (finished_at - started_at).total_seconds(),
            "total_mountains": len(mountain_names),
            "details": details,  # sebagian yang sempat berhasil sebelum error
        }
        log_update_run(summary)
        raise  # tetap dilempar ulang supaya endpoint cron tahu ada kegagalan

    finished_at = datetime.utcnow()
    success_count = sum(1 for d in details if d["forecast_ok"] and d["history_ok"])
    partial_count = sum(1 for d in details if d["forecast_ok"] != d["history_ok"])
    failed_count = sum(1 for d in details if not d["forecast_ok"] and not d["history_ok"])

    summary = {
        "status": "success" if failed_count == 0 and partial_count == 0 else "partial_failure",
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "total_mountains": len(mountain_names),
        "success_count": success_count,
        "partial_count": partial_count,
        "failed_count": failed_count,
        "details": details,
    }
    log_update_run(summary)
    print(f"[WEATHER][SUMMARY] {summary['status']}: "
          f"{success_count} sukses, {partial_count} sebagian, {failed_count} gagal "
          f"({summary['duration_seconds']:.1f}s)")
    return summary


# ==============================================================================
# 5. FUNGSI BACA - dipanggil oleh endpoint API mobile
# ==============================================================================
def get_forecast_by_name(mountain_name: str):
    mountain_name = normalize_name(mountain_name)
    return get_weather_db()["weather_forecast"].find_one(
        {"mountain_name": mountain_name}, {"_id": 0}
    )


def get_history_by_name(mountain_name: str, limit_days: int = 30):
    mountain_name = normalize_name(mountain_name)
    doc = get_weather_db()["weather_history"].find_one(
        {"mountain_name": mountain_name}, {"_id": 0}
    )
    if doc and limit_days:
        cutoff = (datetime.utcnow() - timedelta(days=limit_days)).isoformat()
        doc["history"] = [h for h in doc["history"] if h["datetime"] >= cutoff]
    return doc