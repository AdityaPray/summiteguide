import os
from dotenv import load_dotenv
load_dotenv()
from pymongo import MongoClient

uri = os.getenv("MONGO_URI")
client = MongoClient(uri)
db = client["summit_guide_weather"]

print("Jumlah dokumen di weather_history:", db["weather_history"].count_documents({}))
doc = db["weather_history"].find_one({"mountain_name": "gunung slamet"})
print("Hasil pencarian history 'gunung slamet':", doc is not None)
if doc:
    print("Jumlah data jam di history:", len(doc.get("history", [])))