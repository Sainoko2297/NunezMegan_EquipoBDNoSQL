"""Subpaso 6: extrae datos complementarios de las empresas ya cargadas en raw_wikidata.

- raw_wikipedia: resumen (primer párrafo) en español y en inglés, desde la API REST
  de Wikipedia. Licencia CC BY-SA: la interfaz debe mostrar la atribución.
- raw_dbpedia:   año de fundación según DBpedia, como segunda opinión.

Los títulos de los artículos salen de los sitelinks guardados en raw_wikidata.
Es idempotente: cada corrida reemplaza el documento del mismo QID.
"""
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from pymongo import MongoClient, ReplaceOne

ENCABEZADOS = {"User-Agent": "ProyectoEmpresasNoSQL/0.1 (proyecto escolar UPIICSA-IPN)"}
RESUMEN = "https://{idioma}.wikipedia.org/api/rest_v1/page/summary/{titulo}"
DBPEDIA = "https://dbpedia.org/sparql"
RECURSO_DBPEDIA = "http://dbpedia.org/resource/"
TAMANO_LOTE = 50
ANIO = re.compile(r"^(-?\d{1,4})")

CONSULTA_DBPEDIA = """
PREFIX dbo: <http://dbpedia.org/ontology/>
SELECT ?recurso ?anio ?fecha WHERE {
  VALUES ?recurso { __RECURSOS__ }
  OPTIONAL { ?recurso dbo:wikiPageRedirects ?destino }
  BIND(COALESCE(?destino, ?recurso) AS ?real)
  OPTIONAL { ?real dbo:foundingYear ?anio }
  OPTIONAL { ?real dbo:foundingDate ?fecha }
}
"""


def pedir(metodo, url, **opciones):
    """Petición con reintentos ante límite de velocidad o errores del servidor."""
    for _ in range(5):
        respuesta = requests.request(metodo, url, headers=ENCABEZADOS, timeout=60, **opciones)
        if respuesta.status_code == 429 or respuesta.status_code >= 500:
            espera = int(respuesta.headers.get("Retry-After", 10))
            print(f"  Servidor ocupado ({respuesta.status_code}), esperando {espera} s")
            time.sleep(espera)
            continue
        return respuesta
    raise RuntimeError(f"Sin respuesta después de 5 intentos: {url}")


def titulos(raw):
    sitios = raw["entidad"].get("sitelinks", {})
    return {idioma: sitios[f"{idioma}wiki"]["title"]
            for idioma in ("es", "en") if f"{idioma}wiki" in sitios}


# ---------- Wikipedia ----------

def resumen_wikipedia(idioma, titulo):
    """Devuelve (datos, problema). problema es None si todo salió bien."""
    url = RESUMEN.format(idioma=idioma, titulo=quote(titulo.replace(" ", "_"), safe=""))
    respuesta = pedir("GET", url)
    if respuesta.status_code == 404:
        return None, "artículo no encontrado"
    respuesta.raise_for_status()
    datos = respuesta.json()
    if datos.get("type") == "disambiguation":
        return None, "es una página de desambiguación"
    if not datos.get("extract"):
        return None, "sin resumen"
    return {
        "titulo": datos.get("title"),
        "extracto": datos["extract"],
        "descripcion": datos.get("description"),
        "wikibase_item": datos.get("wikibase_item"),
        "revision": datos.get("revision"),
        "url": datos.get("content_urls", {}).get("desktop", {}).get("page"),
    }, None


def extraer_wikipedia(raws, ahora, avisos):
    operaciones = []
    for i, raw in enumerate(raws, 1):
        qid, documento = raw["_id"], {"_id": raw["_id"]}
        for idioma, titulo in titulos(raw).items():
            datos, problema = resumen_wikipedia(idioma, titulo)
            if problema:
                avisos.append(f"wikipedia {idioma} {qid} {raw['nombre_lista']}: {problema}")
                continue
            # Comprobación cruzada: el artículo debe corresponder al mismo QID.
            if datos["wikibase_item"] and datos["wikibase_item"] != qid:
                avisos.append(f"wikipedia {idioma} {qid} {raw['nombre_lista']}: el artículo "
                              f"corresponde a {datos['wikibase_item']}, se descarta")
                continue
            documento[idioma] = datos
            time.sleep(1)  # Wikipedia limita a los clientes sin cuenta; 0.1 s provocaba 429
        documento["extraccion"] = {"fecha": ahora, "fuente": "wikipedia"}
        operaciones.append(ReplaceOne({"_id": qid}, documento, upsert=True))
        if i % 40 == 0:
            print(f"  {i} de {len(raws)}")
    return operaciones


# ---------- DBpedia ----------

def iri_dbpedia(titulo):
    # Caracteres que no se permiten dentro de <...> en SPARQL se codifican.
    return RECURSO_DBPEDIA + quote(titulo.replace(" ", "_"), safe="!$&'()*+,;=:@/-._~")


def extraer_dbpedia(raws, ahora, avisos):
    por_recurso = {}
    for raw in raws:
        titulo = titulos(raw).get("en")
        if titulo:
            por_recurso[iri_dbpedia(titulo)] = raw
        else:
            avisos.append(f"dbpedia {raw['_id']} {raw['nombre_lista']}: sin artículo en inglés")

    resultados = {iri: {"anios": set(), "fechas": set()} for iri in por_recurso}
    recursos = list(por_recurso)
    for inicio in range(0, len(recursos), TAMANO_LOTE):
        lote = recursos[inicio:inicio + TAMANO_LOTE]
        consulta = CONSULTA_DBPEDIA.replace("__RECURSOS__", " ".join(f"<{r}>" for r in lote))
        respuesta = pedir("POST", DBPEDIA, data={"query": consulta,
                                                 "format": "application/sparql-results+json"})
        respuesta.raise_for_status()
        for fila in respuesta.json()["results"]["bindings"]:
            iri = fila["recurso"]["value"]
            if iri not in resultados:
                continue
            for campo, destino in (("anio", "anios"), ("fecha", "fechas")):
                texto = fila.get(campo, {}).get("value", "")
                if texto:
                    resultados[iri][destino].add(texto)
        time.sleep(1)

    operaciones = []
    for iri, raw in por_recurso.items():
        anios = set()
        for texto in resultados[iri]["anios"] | resultados[iri]["fechas"]:
            coincidencia = ANIO.match(texto)
            if coincidencia:
                anios.add(int(coincidencia.group(1)))
        operaciones.append(ReplaceOne({"_id": raw["_id"]}, {
            "_id": raw["_id"],
            "recurso": iri,
            "anios_fundacion": sorted(anios),
            "fechas_fundacion": sorted(resultados[iri]["fechas"]),
            "extraccion": {"fecha": ahora, "fuente": "dbpedia"},
        }, upsert=True))
    return operaciones


def main():
    load_dotenv()
    bd = MongoClient(os.environ["URI_MONGO"])[os.environ["BD_MONGO"]]
    raws = list(bd["raw_wikidata"].find({}, {"nombre_lista": 1, "entidad.sitelinks": 1}))
    ahora = datetime.now(timezone.utc)
    avisos = []

    print(f"Wikipedia: resúmenes de {len(raws)} empresas...")
    operaciones = extraer_wikipedia(raws, ahora, avisos)
    resultado = bd["raw_wikipedia"].bulk_write(operaciones, ordered=False)
    con_es = bd["raw_wikipedia"].count_documents({"es": {"$exists": True}})
    con_en = bd["raw_wikipedia"].count_documents({"en": {"$exists": True}})
    sin_resumen = bd["raw_wikipedia"].count_documents(
        {"es": {"$exists": False}, "en": {"$exists": False}})
    print(f"raw_wikipedia: {resultado.upserted_count} nuevos, {resultado.modified_count} "
          f"actualizados | en español {con_es}, en inglés {con_en}, sin resumen {sin_resumen}")

    print("DBpedia: años de fundación...")
    operaciones = extraer_dbpedia(raws, ahora, avisos)
    if operaciones:
        resultado = bd["raw_dbpedia"].bulk_write(operaciones, ordered=False)
        con_anio = bd["raw_dbpedia"].count_documents({"anios_fundacion.0": {"$exists": True}})
        print(f"raw_dbpedia: {resultado.upserted_count} nuevos, {resultado.modified_count} "
              f"actualizados | con año de fundación {con_anio}")

    for aviso in avisos:
        print("AVISO:", aviso)


if __name__ == "__main__":
    main()
