import csv
import time
from pathlib import Path

import requests

URL = "https://query.wikidata.org/sparql"
ENCABEZADOS = {
    "User-Agent": "ProyectoEmpresasNoSQL/0.1 (proyecto escolar UPIICSA-IPN)",
    "Accept": "application/sparql-results+json",
}
SALIDA = Path(__file__).parent / "datos" / "candidatas.csv"

# (sector, confianza, umbral de sitelinks, QIDs de industria)
GRUPOS = [
    ("computacion", "alta", 15, ["Q1067601", "Q132898730"]),
    ("semiconductores", "alta", 15, ["Q2986369"]),
    ("software", "alta", 15, ["Q880371", "Q638608"]),
    ("internet", "alta", 15, ["Q75", "Q56611700", "Q484847", "Q193424", "Q186165"]),
    ("telecomunicaciones", "alta", 15, ["Q2401742", "Q418", "Q56598901", "Q1780509",
                                        "Q872", "Q1546066", "Q1301371"]),
    ("internet", "baja", 25, ["Q189507", "Q5892272"]),
    ("servicios_ti", "baja", 25, ["Q1540863", "Q1481411"]),
    ("", "baja", 25, ["Q11661", "Q5268834", "Q11492876", "Q110702998", "Q21157865"]),
]

PLANTILLA = """
SELECT ?empresa ?empresaLabel ?enlaces
       (MIN(YEAR(?f)) AS ?fundacion) (MIN(YEAR(?d)) AS ?disolucion)
       (GROUP_CONCAT(DISTINCT ?tipoNombre; separator=" | ") AS ?tipos)
WHERE {
  VALUES ?industria { __INDUSTRIAS__ }
  ?empresa wdt:P452 ?industria ;
           wikibase:sitelinks ?enlaces .
  FILTER(?enlaces >= __UMBRAL__)
  OPTIONAL { ?empresa wdt:P571 ?f }
  OPTIONAL { ?empresa wdt:P576 ?d }
  OPTIONAL { ?empresa wdt:P31 ?tipo }
  SERVICE wikibase:label {
    bd:serviceParam wikibase:language "es,mul,en".
    ?empresa rdfs:label ?empresaLabel .
    ?tipo rdfs:label ?tipoNombre .
  }
}
GROUP BY ?empresa ?empresaLabel ?enlaces
HAVING (COUNT(?d) = 0 || MIN(YEAR(?d)) >= 1947)
"""

PROYECTO = ["fundación", "open-source project", "proyecto", "software libre", "sistema operativo"]
DESCARTE = ["emisora", "difusora", "radiodifusión", "cadena de televisión", "cadena de radio",
            "televisión por", "agencia de noticias", "organización de estándares", "estándar",
            "standard", "organismo público", "cadena de tiendas", "cadena de supermercados",
            "hipermercado", "cadena de ferreterías", "sistema de pago", "institución financiera", "bolsa"]
DESCARTE_EXACTO = {"industria", "rama de la ciencia", "disciplina académica", "tecnología",
                   "protocolo de red", "type of business or company", "objective quality metric",
                   "generación de telefonía móvil"}
REVISAR = ["videojuego", "video game", "filial", "división", "sin fines de lucro"]
TIPO_EMPRESA = ["empresa", "negocio", "compañía", "corporación", "holding", "conglomerado",
                "sociedad", "multinacional", "gaisha", "konzern", "kombinat", "operador",
                "proveedor", "fabricante", "organización comercial", "grupo de empresas",
                "integrador", "registrador", "red de entrega", "supplier", "manufacturer",
                "associated company", "personalidad jurídica", "firm"]


def consultar(industrias, umbral):
    consulta = (PLANTILLA
                .replace("__INDUSTRIAS__", " ".join(f"wd:{q}" for q in industrias))
                .replace("__UMBRAL__", str(umbral)))
    for _ in range(3):
        respuesta = requests.get(URL, params={"query": consulta},
                                 headers=ENCABEZADOS, timeout=90)
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


def sugerir(tipos):
    """Devuelve (sugerencia, motivo). El orden de las reglas importa."""
    texto = tipos.lower()
    fichas = {t.strip() for t in texto.split("|")}
    for p in PROYECTO:
        if p in texto:
            return "proyecto", p
    for p in DESCARTE:
        if p in texto:
            return "descartar", p
    exactos = fichas & DESCARTE_EXACTO
    if exactos:
        return "descartar", sorted(exactos)[0]
    for p in REVISAR:
        if p in texto:
            return "revisar", p
    if not any(p in texto for p in TIPO_EMPRESA):
        return "revisar", "sin tipo de empresa"
    return "empresa", ""


def main():
    candidatas = {}
    for sector, confianza, umbral, industrias in GRUPOS:
        print(f"Consultando {sector or 'genéricas'} ({confianza})...")
        filas = consultar(industrias, umbral)
        print(f"  {len(filas)} filas")
        for fila in filas:
            qid = valor(fila, "empresa").rsplit("/", 1)[-1]
            c = candidatas.setdefault(qid, {
                "qid": qid,
                "nombre": valor(fila, "empresaLabel"),
                "sectores": set(),
                "confianza": "baja",
                "enlaces": int(valor(fila, "enlaces")),
                "fundacion": valor(fila, "fundacion"),
                "disolucion": valor(fila, "disolucion"),
                "tipos": valor(fila, "tipos"),
            })
            if sector:
                c["sectores"].add(sector)
            if confianza == "alta":
                c["confianza"] = "alta"
        time.sleep(2)

    SALIDA.parent.mkdir(exist_ok=True)
    columnas = ["qid", "nombre", "sectores", "confianza", "enlaces", "fundacion",
                "disolucion", "tipos", "sugerencia", "motivo", "decision", "notas"]
    ordenadas = sorted(candidatas.values(),
                       key=lambda c: (c["confianza"] != "alta", -c["enlaces"]))
    with SALIDA.open("w", newline="", encoding="utf-8-sig") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=columnas)
        escritor.writeheader()
        for c in ordenadas:
            sugerencia, motivo = sugerir(c["tipos"])
            escritor.writerow({**c,
                               "sectores": "|".join(sorted(c["sectores"])),
                               "sugerencia": sugerencia, "motivo": motivo,
                               "decision": "", "notas": ""})
    print(f"\n{len(candidatas)} candidatas guardadas en {SALIDA}")


if __name__ == "__main__":
    main()