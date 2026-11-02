"""Subpaso 4: extrae de Wikidata las empresas incluidas y las guarda en raw_wikidata.

Entrada: datos/lista_semilla.csv (solo filas con decision = incluir).
Salida:  colección raw_wikidata, un documento por QID, con la entidad tal como
         la entrega Wikidata (reclamos con rango, precisión y calificadores)
         más las etiquetas de las entidades que menciona (ciudades, países,
         fundadores, industrias, matrices) y las coordenadas de la sede.

Es idempotente: cada corrida reemplaza el documento del mismo QID.
La capa raw no se edita a mano; las correcciones van en override de empresas.
"""
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from pymongo import MongoClient, ReplaceOne

API = "https://www.wikidata.org/w/api.php"
ENCABEZADOS = {"User-Agent": "ProyectoEmpresasNoSQL/0.1 (proyecto escolar UPIICSA-IPN)"}
LISTA = Path(__file__).parent / "datos" / "lista_semilla.csv"
TAMANO_LOTE = 50           # máximo que acepta wbgetentities por petición
IDIOMAS = "es|mul|en"
SITIOS = "eswiki|enwiki"   # enwiki se usa después para buscar en DBpedia

# Propiedades cuyas entidades referenciadas necesitan etiqueta legible.
PROPIEDADES_REFERENCIA = [
    "P31",    # instancia de
    "P17",    # país
    "P159",   # sede
    "P740",   # lugar de fundación
    "P112",   # fundadores
    "P452",   # industria
    "P127",   # propiedad de
    "P749",   # organización matriz
    "P355",   # filial
    "P1365",  # reemplaza a
    "P1366",  # reemplazada por
]
HUMANO = "Q5"

INTENTOS = 8


def solicitar(parametros):
    parametros = {**parametros, "format": "json", "maxlag": 5}
    for intento in range(1, INTENTOS + 1):
        respuesta = requests.get(API, params=parametros, headers=ENCABEZADOS, timeout=60)
        if respuesta.status_code == 429 or respuesta.status_code >= 500:
            espera = int(respuesta.headers.get("Retry-After", 10))
            print(f"  Wikidata ocupado ({respuesta.status_code}), esperando {espera} s")
            time.sleep(espera)
            continue
        respuesta.raise_for_status()
        datos = respuesta.json()
        if "error" in datos:
            if datos["error"].get("code") == "maxlag":
                espera = max(int(respuesta.headers.get("Retry-After", 5)), 5 * intento)
                print(f"  Wikidata con retraso de {datos['error'].get('lag', '?')} s, "
                      f"intento {intento} de {INTENTOS}, esperando {espera} s")
                time.sleep(espera)
                continue
            raise RuntimeError(datos["error"])
        return datos
    raise RuntimeError(f"Wikidata no respondió después de {INTENTOS} intentos")


def lotes(lista, tamano):
    for i in range(0, len(lista), tamano):
        yield lista[i:i + tamano]


def obtener_entidades(qids, props, con_sitios=False):
    """Devuelve {qid solicitado: entidad}. Respeta redirecciones de Wikidata."""
    resultado = {}
    for lote in lotes(list(qids), TAMANO_LOTE):
        parametros = {"action": "wbgetentities", "ids": "|".join(lote),
                      "props": props, "languages": IDIOMAS}
        if con_sitios:
            parametros["sitefilter"] = SITIOS
        for clave, entidad in solicitar(parametros).get("entities", {}).items():
            original = entidad.get("redirects", {}).get("from", clave)
            resultado[original] = entidad
        time.sleep(1)
    return resultado


def qids_referenciados(entidad, propiedades):
    for propiedad in propiedades:
        for reclamo in entidad.get("claims", {}).get(propiedad, []):
            valor = reclamo.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(valor, dict) and valor.get("entity-type") == "item":
                yield valor["id"]


def unidades(entidad, propiedades=("P2139",)):
    """QIDs de las monedas usadas en cantidades (ingresos)."""
    for propiedad in propiedades:
        for reclamo in entidad.get("claims", {}).get(propiedad, []):
            unidad = reclamo.get("mainsnak", {}).get("datavalue", {}).get("value", {})
            unidad = unidad.get("unit", "") if isinstance(unidad, dict) else ""
            if "/entity/Q" in unidad:
                yield unidad.rsplit("/", 1)[-1]


def codigo_iso(entidad):
    """Código ISO 4217 de una moneda (P498), por ejemplo USD o DEM."""
    for reclamo in entidad.get("claims", {}).get("P498", []):
        valor = reclamo.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(valor, str):
            return valor
    return None


def etiqueta(entidad):
    etiquetas = entidad.get("labels", {})
    for idioma in ("es", "mul", "en"):
        if idioma in etiquetas:
            return etiquetas[idioma]["value"]
    return ""


def coordenadas(entidad):
    """Primer P625 como [longitud, latitud], el orden que exige GeoJSON."""
    for reclamo in entidad.get("claims", {}).get("P625", []):
        valor = reclamo.get("mainsnak", {}).get("datavalue", {}).get("value")
        if valor:
            return [valor["longitude"], valor["latitude"]]
    return None


def leer_lista():
    with LISTA.open(encoding="utf-8-sig", newline="") as archivo:
        filas = [f for f in csv.DictReader(archivo) if f["decision"] == "incluir"]
    qids = [f["qid"] for f in filas]
    repetidos = {q for q in qids if qids.count(q) > 1}
    if repetidos:
        raise SystemExit(f"QIDs repetidos en la lista: {sorted(repetidos)}")
    return {f["qid"]: f["nombre"] for f in filas}


def main():
    load_dotenv()
    nombres = leer_lista()
    print(f"Extrayendo {len(nombres)} empresas de Wikidata...")
    entidades = obtener_entidades(nombres, "labels|descriptions|aliases|claims|sitelinks",
                                  con_sitios=True)

    faltantes, personas, redirigidas, validas = [], [], [], {}
    for qid in nombres:
        entidad = entidades.get(qid)
        if entidad is None or "missing" in entidad:
            faltantes.append(qid)
        elif HUMANO in qids_referenciados(entidad, ["P31"]):
            personas.append(qid)
        else:
            if entidad["id"] != qid:
                redirigidas.append((qid, entidad["id"]))
            validas[qid] = entidad

    referencias, sedes, monedas = set(), set(), set()
    for entidad in validas.values():
        referencias.update(qids_referenciados(entidad, PROPIEDADES_REFERENCIA))
        sedes.update(qids_referenciados(entidad, ["P159"]))
        monedas.update(unidades(entidad))

    print(f"Obteniendo etiquetas de {len(referencias)} entidades referenciadas...")
    etiquetas_ref = {q: etiqueta(e) for q, e in obtener_entidades(referencias, "labels").items()}
    print(f"Obteniendo coordenadas de {len(sedes)} sedes...")
    coordenadas_ref = {q: coordenadas(e) for q, e in obtener_entidades(sedes, "claims").items()}
    print(f"Obteniendo códigos ISO de {len(monedas)} monedas...")
    monedas_ref = {q: {"etiqueta": etiqueta(e), "iso4217": codigo_iso(e)}
                   for q, e in obtener_entidades(monedas, "labels|claims").items()}
    sin_iso = sorted(f"{q} {m['etiqueta']}" for q, m in monedas_ref.items() if not m["iso4217"])

    ahora = datetime.now(timezone.utc)
    operaciones = []
    for qid, entidad in validas.items():
        propias = set(qids_referenciados(entidad, PROPIEDADES_REFERENCIA))
        documento = {
            "_id": qid,
            "id_wikidata": entidad["id"],
            "nombre_lista": nombres[qid],
            "entidad": {clave: entidad.get(clave, {}) for clave in
                        ("labels", "descriptions", "aliases", "claims", "sitelinks")},
            "referencias": {
                q: {"etiqueta": etiquetas_ref.get(q, ""),
                    **({"coordenadas": coordenadas_ref[q]} if coordenadas_ref.get(q) else {})}
                for q in sorted(propias)
            },
            "monedas": {q: monedas_ref[q] for q in sorted(set(unidades(entidad)))
                        if q in monedas_ref},
            "extraccion": {"fecha": ahora, "fuente": "wikidata",
                           "revision": entidad.get("lastrevid")},
        }
        operaciones.append(ReplaceOne({"_id": qid}, documento, upsert=True))

    cliente = MongoClient(os.environ["URI_MONGO"])
    coleccion = cliente[os.environ["BD_MONGO"]]["raw_wikidata"]
    if operaciones:
        resultado = coleccion.bulk_write(operaciones, ordered=False)
        print(f"\nraw_wikidata: {resultado.upserted_count} nuevos, "
              f"{resultado.modified_count} actualizados, total {coleccion.count_documents({})}")

    if faltantes:
        print("AVISO: no existen en Wikidata (revisar QID):", ", ".join(faltantes))
    if personas:
        print("AVISO: son personas, no se cargaron (corregir QID):",
              ", ".join(f"{q} {nombres[q]}" for q in personas))
    if sin_iso:
        print("AVISO: monedas sin código ISO (sus ingresos se descartarán):", ", ".join(sin_iso))
    for anterior, actual in redirigidas:
        print(f"AVISO: {anterior} ({nombres[anterior]}) ahora es {actual}; actualizar la lista")
    print(f"Cargadas {len(validas)} de {len(nombres)}")


if __name__ == "__main__":
    main()
