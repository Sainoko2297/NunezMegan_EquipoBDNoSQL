"""Respalda y restaura las correcciones manuales (override) de la colección empresas.

Las correcciones son lo único que no se puede regenerar desde Wikidata, así que se
guardan en datos/overrides.json, que sí se sube al repositorio.

Uso:
  python respaldar_overrides.py exportar    base de datos -> datos/overrides.json
  python respaldar_overrides.py restaurar   datos/overrides.json -> base de datos

Para restaurar, las fichas deben existir: correr antes transformar_empresas.py,
luego restaurar, y luego transformar_empresas.py otra vez para aplicarlas.
La conexión se lee del .env; nunca se pasa por la línea de comandos.
"""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

ARCHIVO = Path(__file__).parent / "datos" / "overrides.json"


def exportar(coleccion):
    datos = {d["_id"]: d["override"] for d in
             coleccion.find({"override": {"$exists": True}}, {"override": 1}).sort("_id")}
    ARCHIVO.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(datos)} correcciones exportadas a {ARCHIVO}")


def restaurar(coleccion):
    datos = json.loads(ARCHIVO.read_text(encoding="utf-8"))
    aplicadas, faltantes = 0, []
    for qid, override in datos.items():
        resultado = coleccion.update_one({"_id": qid}, {"$set": {"override": override}})
        if resultado.matched_count:
            aplicadas += 1
        else:
            faltantes.append(qid)
    print(f"{aplicadas} correcciones restauradas de {len(datos)}")
    if faltantes:
        print("AVISO: sin ficha en empresas (correr antes transformar_empresas.py):",
              ", ".join(faltantes))


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("exportar", "restaurar"):
        raise SystemExit("Uso: python respaldar_overrides.py exportar|restaurar")
    load_dotenv()
    coleccion = MongoClient(os.environ["URI_MONGO"])[os.environ["BD_MONGO"]]["empresas"]
    exportar(coleccion) if sys.argv[1] == "exportar" else restaurar(coleccion)


if __name__ == "__main__":
    main()
