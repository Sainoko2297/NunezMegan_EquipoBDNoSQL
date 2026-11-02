import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
cliente = MongoClient(os.environ["URI_MONGO"], serverSelectionTimeoutMS=3000)
print(cliente.admin.command("ping"))
print("Versión del servidor:", cliente.server_info()["version"])