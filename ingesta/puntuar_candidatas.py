"""Puntúa las candidatas y preselecciona las empresas del catálogo por cuota de sector.

Entrada: datos/candidatas.csv (generado por buscar_candidatas.py, sin editar).
Salida:  datos/candidatas_puntuadas.csv, para revisión humana.

Criterios automáticos (guía de curación, criterios 2 a 5):
  - cobertura temporal y empresas desaparecidas,
  - conexiones con otras candidatas (adquisiciones, matrices, sucesiones),
  - diversidad geográfica (tope por país),
  - número de enlaces como medida de relevancia.
El criterio 1 (aporte histórico) no se automatiza: lo aplica quien revisa.
"""
import csv
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

URL = "https://query.wikidata.org/sparql"
ENCABEZADOS = {
    "User-Agent": "ProyectoEmpresasNoSQL/0.1 (proyecto escolar UPIICSA-IPN)",
    "Accept": "application/sparql-results+json",
}
CARPETA = Path(__file__).parent / "datos"
ENTRADA = CARPETA / "candidatas.csv"
SALIDA = CARPETA / "candidatas_puntuadas.csv"

CUOTAS = {
    "software": 35,
    "computacion": 30,
    "internet": 30,
    "telecomunicaciones": 30,
    "semiconductores": 25,
    "servicios_ti": 10,
}
PROPORCION_DESAPARECIDAS = 1 / 3   # mínimo por sector
TOPE_PAIS = 0.40                   # máximo por país dentro de cada sector
TAMANO_BORDE = 5                   # filas que quedan justo debajo del corte
TAMANO_LOTE = 150                  # QIDs por consulta a Wikidata

RELACIONES = {
    "P127": "propiedad de",
    "P749": "organización matriz",
    "P355": "filial",
    "P1830": "dueña de",
    "P199": "división",
    "P1365": "reemplaza a",
    "P1366": "reemplazada por",
}

# Asignación de sector para las candidatas genéricas, por palabras en sus tipos.
SECTOR_POR_TIPO = [
    ("consultor", "servicios_ti"),
    ("professional services", "servicios_ti"),
    ("telecom", "telecomunicaciones"),
    ("semiconductor", "semiconductores"),
    ("software", "software"),
    ("computación", "computacion"),
    ("computer", "computacion"),
    ("en línea", "internet"),
    ("sitio web", "internet"),
    ("internet", "internet"),
]

CONSULTA_PAIS = """
SELECT ?e ?orden ?paisNombre WHERE {
  VALUES ?e { __QIDS__ }
  { ?e wdt:P17 ?pais . BIND(1 AS ?orden) }
  UNION
  { ?e wdt:P159 ?sede . ?sede wdt:P17 ?pais . BIND(2 AS ?orden) }
  SERVICE wikibase:label {
    bd:serviceParam wikibase:language "es,mul,en".
    ?pais rdfs:label ?paisNombre .
  }
}
"""

CONSULTA_RELACIONES = """
SELECT ?a ?propiedad ?b WHERE {
  VALUES ?a { __QIDS__ }
  VALUES ?propiedad { __PROPIEDADES__ }
  ?a ?propiedad ?b .
}
"""


def consultar(consulta):
    # POST en lugar de GET: con 150 QIDs la URL sería demasiado larga.
    for _ in range(3):
        respuesta = requests.post(URL, data={"query": consulta},
                                  headers=ENCABEZADOS, timeout=120)
        if respuesta.status_code == 429:
            espera = int(respuesta.headers.get("Retry-After", 30))
            print(f"  Límite de peticiones, esperando {espera} s")
            time.sleep(espera)
            continue
        respuesta.raise_for_status()
        return respuesta.json()["results"]["bindings"]
    raise RuntimeError("Wikidata rechazó la consulta tres veces")


def valor(fila, campo):
    return fila.get(campo, {}).get("value", "")


def qid_de(uri):
    return uri.rsplit("/", 1)[-1]


def lotes(lista, tamano):
    for i in range(0, len(lista), tamano):
        yield lista[i:i + tamano]


def obtener_paises(qids):
    """País por QID: primero P17 de la empresa; si no tiene, el de su sede."""
    mejor = {}
    for lote in lotes(qids, TAMANO_LOTE):
        consulta = CONSULTA_PAIS.replace("__QIDS__", " ".join(f"wd:{q}" for q in lote))
        for fila in consultar(consulta):
            qid = qid_de(valor(fila, "e"))
            orden = int(valor(fila, "orden"))
            if qid not in mejor or orden < mejor[qid][0]:
                mejor[qid] = (orden, valor(fila, "paisNombre"))
        time.sleep(2)
    return {qid: pais for qid, (_, pais) in mejor.items()}


def obtener_relaciones(qids):
    """Relaciones (a, propiedad, b) donde a y b son candidatas distintas."""
    universo = set(qids)
    propiedades = " ".join(f"wdt:{p}" for p in RELACIONES)
    relaciones = set()
    for lote in lotes(qids, TAMANO_LOTE):
        consulta = (CONSULTA_RELACIONES
                    .replace("__QIDS__", " ".join(f"wd:{q}" for q in lote))
                    .replace("__PROPIEDADES__", propiedades))
        for fila in consultar(consulta):
            a = qid_de(valor(fila, "a"))
            b = qid_de(valor(fila, "b"))
            if b in universo and a != b:
                relaciones.add((a, qid_de(valor(fila, "propiedad")), b))
        time.sleep(2)
    return relaciones


def sector_para(c):
    if c["sectores"]:
        return c["sectores"].split("|")[0]
    texto = c["tipos"].lower()
    for palabra, sector in SECTOR_POR_TIPO:
        if palabra in texto:
            return sector
    return ""


def puntuar(c, max_enlaces):
    puntos = 40 * math.log(int(c["enlaces"])) / math.log(max_enlaces)
    puntos += min(c["conexiones"], 3) * 10
    if c["fundacion"]:
        anio = int(c["fundacion"])
        if anio < 1970:
            puntos += 15
        elif anio < 1990:
            puntos += 8
    if c["disolucion"]:
        puntos += 10
    if c["confianza"] == "baja":
        puntos -= 10
    if c["sugerencia"] == "revisar":
        puntos -= 25 if c["motivo"] == "sin tipo de empresa" else 15
    return round(puntos, 1)


def seleccionar(filas, cuota, sector, avisos):
    minimo_desaparecidas = math.ceil(cuota * PROPORCION_DESAPARECIDAS)
    tope = max(1, math.floor(cuota * TOPE_PAIS))
    ordenadas = sorted(filas, key=lambda c: -c["puntuacion"])
    elegidas = []
    usados = set()
    por_pais = Counter()

    def cabe(c):
        return not c["pais"] or por_pais[c["pais"]] < tope

    def elegir(c):
        elegidas.append(c)
        usados.add(c["qid"])
        por_pais[c["pais"]] += 1

    # Fase 1: asegurar el mínimo de desaparecidas o absorbidas.
    for c in ordenadas:
        if len(elegidas) >= minimo_desaparecidas:
            break
        if c["disolucion"] and cabe(c):
            elegir(c)
    if len(elegidas) < minimo_desaparecidas:
        avisos.append(f"{sector}: solo {len(elegidas)} desaparecidas de "
                      f"{minimo_desaparecidas} requeridas")

    # Fase 2: completar la cuota por puntuación, respetando el tope por país.
    for c in ordenadas:
        if len(elegidas) >= cuota:
            break
        if c["qid"] not in usados and cabe(c):
            elegir(c)

    # Fase 3: si el tope impidió llenar la cuota, se relaja y se avisa.
    if len(elegidas) < cuota:
        antes = len(elegidas)
        for c in ordenadas:
            if len(elegidas) >= cuota:
                break
            if c["qid"] not in usados:
                elegir(c)
        if len(elegidas) > antes:
            avisos.append(f"{sector}: se relajó el tope por país para "
                          f"{len(elegidas) - antes} filas")
    if len(elegidas) < cuota:
        avisos.append(f"{sector}: faltan {cuota - len(elegidas)} empresas; "
                      f"completar con la lista semilla manual")

    borde = [c for c in ordenadas if c["qid"] not in usados][:TAMANO_BORDE]
    return elegidas, borde


def main():
    with ENTRADA.open(encoding="utf-8-sig", newline="") as archivo:
        candidatas = list(csv.DictReader(archivo))
    por_qid = {c["qid"]: c for c in candidatas}
    qids = list(por_qid)

    print(f"Consultando país de {len(qids)} candidatas...")
    paises = obtener_paises(qids)
    print("Consultando relaciones entre candidatas...")
    relaciones = obtener_relaciones(qids)
    print(f"  {len(relaciones)} relaciones encontradas")

    textos = defaultdict(set)
    vecinos = defaultdict(set)
    for a, propiedad, b in relaciones:
        texto = RELACIONES[propiedad]
        textos[a].add(f"{texto} {b} ({por_qid[b]['nombre']})")
        textos[b].add(f"{a} ({por_qid[a]['nombre']}) {texto} esta")
        vecinos[a].add(b)
        vecinos[b].add(a)

    max_enlaces = max(int(c["enlaces"]) for c in candidatas)
    for c in candidatas:
        c["pais"] = paises.get(c["qid"], "")
        c["sector_cuota"] = sector_para(c)
        c["conexiones"] = len(vecinos[c["qid"]])
        c["relaciones_detectadas"] = " | ".join(sorted(textos[c["qid"]]))
        c["puntuacion"] = puntuar(c, max_enlaces)
        c["seleccion"] = ""
        c["decision"] = "proyecto" if c["sugerencia"] == "proyecto" else ""

    avisos = []
    print("\nSector               elegidas  desaparecidas  país principal")
    for sector, cuota in CUOTAS.items():
        filas = [c for c in candidatas
                 if c["sector_cuota"] == sector
                 and c["sugerencia"] in ("empresa", "revisar")]
        elegidas, borde = seleccionar(filas, cuota, sector, avisos)
        for c in elegidas:
            c["seleccion"] = "preseleccionada"
            c["decision"] = "incluir"
        for c in borde:
            c["seleccion"] = "borde"
        desaparecidas = sum(1 for c in elegidas if c["disolucion"])
        pais, veces = (Counter(c["pais"] for c in elegidas).most_common(1) or [("", 0)])[0]
        print(f"{sector:20} {len(elegidas):3}/{cuota:<4} {desaparecidas:8}"
              f"        {pais or 'sin país'} ({veces})")

    sin_sector = [c for c in candidatas if not c["sector_cuota"]
                  and c["sugerencia"] in ("empresa", "revisar")]
    for c in sin_sector:
        c["seleccion"] = "sin sector"

    orden_sector = {s: i for i, s in enumerate(CUOTAS)}
    orden_seleccion = {"preseleccionada": 0, "borde": 1, "sin sector": 3}
    candidatas.sort(key=lambda c: (orden_sector.get(c["sector_cuota"], len(CUOTAS)),
                                   orden_seleccion.get(c["seleccion"], 2),
                                   -c["puntuacion"]))

    columnas = ["qid", "nombre", "sector_cuota", "seleccion", "decision", "notas",
                "puntuacion", "enlaces", "fundacion", "disolucion", "pais",
                "conexiones", "relaciones_detectadas", "sectores", "confianza",
                "tipos", "sugerencia", "motivo"]
    with SALIDA.open("w", newline="", encoding="utf-8-sig") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=columnas, extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(candidatas)

    print(f"\n{len(sin_sector)} candidatas sin sector asignable (seleccion = 'sin sector')")
    for aviso in avisos:
        print("AVISO:", aviso)
    print(f"\nArchivo generado: {SALIDA}")


if __name__ == "__main__":
    main()
