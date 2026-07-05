"""
storage_service.py
===================
Helper untuk upload file (foto profil, KTP, surat tugas, GPX, foto alat)
ke Supabase Storage, MENGGANTIKAN penyimpanan lokal (photo.save(filepath)).

Kenapa perlu ini: di Vercel, filesystem bersifat read-only dan folder
/tmp dihapus lagi setiap instance daur ulang. File yang disimpan dengan
photo.save() ke folder static/uploads/... akan HILANG. Supabase Storage
menyimpan file secara permanen di luar server, jadi aman dipakai di
lingkungan serverless.

Setup yang dibutuhkan di Supabase Dashboard:
1. Buka menu Storage -> New bucket
2. Nama bucket: "uploads", centang "Public bucket"

Cara pakai (menggantikan pola lama):
    # SEBELUM (tidak aman di Vercel):
    photo.save(filepath)
    user.profile_photo = f"/{filepath}"

    # SESUDAH:
    from storage_service import upload_file_to_supabase
    url = upload_file_to_supabase(photo, folder="profiles")
    user.profile_photo = url
"""

import os
import uuid
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BUCKET_NAME = "uploads"

_supabase_client = None


def get_supabase_client():
    """Lazy init supaya tidak connect saat modul di-import."""
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_KEY belum diset di .env atau Environment Variables Vercel"
            )
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


def upload_file_to_supabase(file_storage, folder: str) -> str:
    """
    file_storage: objek dari request.files.get(...) (Werkzeug FileStorage)
    folder: subfolder logis di dalam bucket, contoh:
            "profiles"   -> foto profil user
            "documents"  -> KTP & surat tugas
            "gpx"        -> file jalur pendakian
            "equipments" -> foto alat rental

    return: URL publik file yang sudah diupload, untuk disimpan ke kolom
            *_photo / *_image / gpx_data di database (menggantikan path lokal)
    """
    if not file_storage or file_storage.filename == "":
        return None

    original_name = file_storage.filename
    ext = original_name.rsplit(".", 1)[-1] if "." in original_name else "bin"
    unique_path = f"{folder}/{uuid.uuid4().hex}.{ext}"

    file_bytes = file_storage.read()

    client = get_supabase_client()
    client.storage.from_(BUCKET_NAME).upload(
        unique_path,
        file_bytes,
        {"content-type": file_storage.mimetype or "application/octet-stream"},
    )

    public_url = client.storage.from_(BUCKET_NAME).get_public_url(unique_path)
    return public_url


def delete_file_from_supabase(file_url: str, folder: str) -> bool:
    """
    Opsional: hapus file lama dari Supabase Storage, misal saat user ganti
    foto profil dan foto lama tidak dipakai lagi (supaya storage tidak
    penuh dengan file yatim). file_url adalah public URL yang tersimpan
    di database.
    """
    if not file_url or f"/{folder}/" not in file_url:
        return False
    try:
        path_in_bucket = file_url.split(f"/{BUCKET_NAME}/")[-1]
        client = get_supabase_client()
        client.storage.from_(BUCKET_NAME).remove([path_in_bucket])
        return True
    except Exception as e:
        print(f"[STORAGE] Gagal hapus file lama: {e}")
        return False
