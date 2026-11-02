import os
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, TEXT, GEOSPHERE

load_dotenv()

SECTORES = ["computacion", "semiconductores", "software", "internet", "telecomunicaciones", "servicios_ti"]
ESTADOS = ["activa", "adquirida", "fusionada", "disuelta"]
TIPOS_HITO = ["lanzamiento", "salida_bolsa", "cambio_sector", "cambio_nombre",
              "fusion", "adquisicion", "venta_division", "otro"]
REGLAS_ALERTA = [
    "fundacion_posterior_disolucion",   # fundación después de la disolución
    "fundacion_posterior_primer_dato",  # fundación después del primer hito o métrica
    "fundaciones_dispersas",            # varias fundaciones a más de 5 años entre sí
    "estado_corregido",                 # adquirida en el año de disolución: estado = adquirida
    "discrepancia_dbpedia",             # año de fundación distinto en DBpedia
    "fecha_dudosa",                     # marcada en notas de la lista semilla
]
ENTERO = {"bsonType": "int"}
NUMERO = {"bsonType": ["int", "long", "double"]}
TEXTO = {"bsonType": "string", "minLength": 1}
QID = {"bsonType": "string", "pattern": "^Q[0-9]+$"}


def referencia():
    """Relación con otra empresa o null. {empresa_id, anio} si la otra empresa está
    en el catálogo; {nombre, anio} si no lo está. Nunca ambas."""
    objeto = {"bsonType": "object", "additionalProperties": False,
              "properties": {"empresa_id": QID, "nombre": TEXTO, "anio": ENTERO}}
    return {"oneOf": [
        {"bsonType": "null"},
        {**objeto, "required": ["empresa_id"]},
        {**objeto, "required": ["nombre"]},
    ]}


def fecha_historica(permite_nulo):
    return {
        "bsonType": ["object", "null"] if permite_nulo else "object",
        "required": ["anio", "precision"],
        "properties": {
            "anio": ENTERO,
            "fecha": {"bsonType": ["date", "null"]},
            "precision": {"enum": ["dia", "mes", "anio", "decada"]},
            "fuente": TEXTO,
            "lugar": {"bsonType": "object",
                      "properties": {"ciudad": TEXTO, "pais": TEXTO}},
        },
    }


def serie(con_moneda):
    requeridos = ["anio", "valor"] + (["moneda"] if con_moneda else [])
    return {
        "bsonType": "array",
        "items": {
            "bsonType": "object",
            "required": requeridos,
            "properties": {
                "anio": ENTERO,
                "valor": NUMERO,
                "moneda": {"bsonType": "string", "pattern": "^[A-Z]{3}$"},
            },
        },
    }


ESQUEMA = {"$jsonSchema": {
    "bsonType": "object",
    "required": ["_id", "nombre", "sectores", "estado", "fundacion", "meta"],
    "additionalProperties": False,
    "properties": {
        "_id": QID,
        "nombre": TEXTO,
        "nombres_historicos": {"bsonType": "array", "items": {
            "bsonType": "object", "required": ["nombre"],
            "properties": {"nombre": TEXTO, "desde": ENTERO, "hasta": ENTERO}}},
        "sectores": {"bsonType": "array", "minItems": 1, "uniqueItems": True,
                     "items": {"enum": SECTORES}},
        "estado": {"enum": ESTADOS},
        "fundacion": fecha_historica(permite_nulo=False),
        "disolucion": fecha_historica(permite_nulo=True),
        "sede": {"bsonType": "object", "properties": {
            "ciudad": TEXTO, "pais": TEXTO,
            "ubicacion": {"bsonType": "object", "required": ["type", "coordinates"],
                          "properties": {
                              "type": {"enum": ["Point"]},
                              "coordinates": {"bsonType": "array", "minItems": 2,
                                              "maxItems": 2, "items": NUMERO}}}}},
        "fundadores": {"bsonType": "array", "items": {
            "bsonType": "object", "required": ["nombre"],
            "properties": {"nombre": TEXTO, "qid": QID}}},
        "relaciones": {"bsonType": "object", "properties": {
            "adquirida_por": referencia(), "escindida_de": referencia(),
            "predecesor": referencia(), "sucesor": referencia()}},
        "metricas": {"bsonType": "object", "properties": {
            "empleados": serie(con_moneda=False),
            "ingresos": serie(con_moneda=True)}},
        "atributos": {"bsonType": "object"},
        "hitos": {"bsonType": "array", "items": {
            "bsonType": "object", "required": ["anio", "tipo", "descripcion"],
            "properties": {"anio": ENTERO, "tipo": {"enum": TIPOS_HITO},
                           "descripcion": TEXTO, "contraparte_id": QID}}},
        "resumen": {"bsonType": "object", "required": ["texto", "idioma", "fuente"],
                    "properties": {"texto": TEXTO, "idioma": {"enum": ["es", "en"]},
                                   "fuente": TEXTO}},
        "override": {"bsonType": "object", "required": ["motivo"],
                "additionalProperties": False,
                "properties": {"campos": {"bsonType": "object"},
                            "agregar": {"bsonType": "object"},
                            "motivo": TEXTO}},
                "meta": {"bsonType": "object", "required": ["version_esquema", "actualizado_en"],
                 "properties": {"version_esquema": ENTERO,
                                "fuentes": {"bsonType": "array", "items": TEXTO},
                                "actualizado_en": {"bsonType": "date"},
                                "alertas": {"bsonType": "array", "items": {
                                    "bsonType": "object", "required": ["regla", "detalle"],
                                    "properties": {"regla": {"enum": REGLAS_ALERTA},
                                                   "detalle": TEXTO}}}}},
    },
}}

INDICES = [
    ([("fundacion.anio", ASCENDING), ("disolucion.anio", ASCENDING)], {"name": "idx_vida_por_anio"}),
    ([("sectores", ASCENDING)], {"name": "idx_sectores"}),
    ([("relaciones.adquirida_por.empresa_id", ASCENDING)], {"name": "idx_adquirida_por"}),
    ([("relaciones.escindida_de.empresa_id", ASCENDING)], {"name": "idx_escindida_de"}),
    ([("nombre", TEXT), ("nombres_historicos.nombre", TEXT)],
     {"name": "idx_texto_nombres", "default_language": "none"}),
    ([("sede.ubicacion", GEOSPHERE)], {"name": "idx_ubicacion"}),
]


def main():
    cliente = MongoClient(os.environ["URI_MONGO"])
    bd = cliente[os.environ["BD_MONGO"]]
    opciones = {"validator": ESQUEMA, "validationLevel": "strict", "validationAction": "error"}

    if "empresas" in bd.list_collection_names():
        bd.command("collMod", "empresas", **opciones)
        print("Validador actualizado")
    else:
        bd.create_collection("empresas", **opciones)
        print("Colección creada")

    coleccion = bd["empresas"]
    for llaves, extra in INDICES:
        print("Índice listo:", coleccion.create_index(llaves, **extra))


if __name__ == "__main__":
    main()