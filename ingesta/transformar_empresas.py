"""Subpaso 5: transforma raw_wikidata en la colección curada empresas.

Parte 1: nombre, sectores, fundación, disolución, sede.
Parte 2: fundadores, relaciones, notas de la lista semilla, hitos y estado.
Parte 3: métricas de empleados e ingresos.
Parte 4: override (correcciones manuales) y alertas.
Parte 5: resumen de Wikipedia y contraste del año de fundación con DBpedia.

Orden: Wikidata -> notas de la lista semilla -> override -> hitos y estado -> alertas.

Prioridad de fuentes en relaciones: notas de la lista semilla > Wikidata.

Uso:
  python transformar_empresas.py            transforma y guarda las 160 empresas
  python transformar_empresas.py Q37156     solo muestra el documento de IBM, sin guardar
"""
import csv
import json
import os
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import WriteError

LISTA = Path(__file__).parent / "datos" / "lista_semilla.csv"
VERSION_ESQUEMA = 1
SECTORES = {"computacion", "semiconductores", "software", "internet",
            "telecomunicaciones", "servicios_ti"}
PRECISIONES = {11: "dia", 10: "mes", 9: "anio", 8: "decada"}
FORMATO_TIEMPO = re.compile(r"^([+-])(\d+)-(\d{2})-(\d{2})")
TIPOS_HITO = {"lanzamiento", "salida_bolsa", "cambio_sector", "cambio_nombre",
              "fusion", "adquisicion", "venta_division", "otro"}

# Formato fijo de notas (guía de curación). Varias entradas separadas por " | ".
NOTA_RELACION = re.compile(r"^(adquirida por|escindida de|fusionada con) (Q\d+) \((\d{4})\)$")
NOTA_FUSIONAR = re.compile(r"^fusionar con (Q\d+) como nombre historico$")
NOTA_HITO = re.compile(r"^hito de (Q\d+): (\w+) (\d{4})(?: desde (Q\d+))?$")
CAMPO_RELACION = {"adquirida por": "adquirida_por", "escindida de": "escindida_de",
                  "fusionada con": "sucesor"}


# ---------- lectura de reclamos de Wikidata ----------

def reclamos(entidad, propiedad):
    """Reclamos con valor, sin los marcados como obsoletos, los preferidos primero."""
    validos = [r for r in entidad["claims"].get(propiedad, [])
               if r.get("rank") != "deprecated"
               and r.get("mainsnak", {}).get("snaktype") == "value"]
    return sorted(validos, key=lambda r: r.get("rank") != "preferred")


def valor(reclamo):
    return reclamo["mainsnak"]["datavalue"]["value"]


def items(entidad, propiedad):
    """QIDs de los reclamos de una propiedad, en orden de preferencia."""
    return [valor(r)["id"] for r in reclamos(entidad, propiedad)
            if isinstance(valor(r), dict) and valor(r).get("entity-type") == "item"]


def leer_tiempo(reclamo):
    v = valor(reclamo)
    coincidencia = FORMATO_TIEMPO.match(v["time"])
    if not coincidencia:
        return None
    signo, anio, mes, dia = coincidencia.groups()
    anio = int(anio) * (-1 if signo == "-" else 1)
    precision = v["precision"]
    fecha = None
    if precision >= 11 and int(mes) and int(dia):
        fecha = datetime(anio, int(mes), int(dia), tzinfo=timezone.utc)
    return {
        "anio": anio,
        "fecha": fecha,
        "precision": PRECISIONES.get(precision, "dia" if precision > 11 else "decada"),
        "preferida": reclamo.get("rank") == "preferred",
        "nivel": precision,
    }


def elegir_fecha(entidad, propiedad):
    """Selección determinista: rango preferido, luego mayor precisión, luego la más antigua.
    Devuelve (fecha elegida o None, lista de todos los años encontrados)."""
    candidatas = [t for t in (leer_tiempo(r) for r in reclamos(entidad, propiedad)) if t]
    if not candidatas:
        return None, []
    elegida = min(candidatas, key=lambda t: (not t["preferida"], -t["nivel"], t["anio"]))
    resultado = {"anio": elegida["anio"], "fecha": elegida["fecha"],
                 "precision": elegida["precision"], "fuente": "wikidata"}
    return resultado, sorted({t["anio"] for t in candidatas})


def etiqueta(entidad, respaldo):
    etiquetas = entidad.get("labels", {})
    for idioma in ("es", "mul", "en"):
        if idioma in etiquetas:
            return etiquetas[idioma]["value"]
    return respaldo


# ---------- construcción del documento ----------

def construir_sectores(fila):
    sectores = [fila["sector_cuota"]]
    sectores += [s for s in fila.get("sectores", "").split("|") if s]
    return [s for s in dict.fromkeys(sectores) if s in SECTORES]


def construir_sede(entidad, referencias):
    paises = items(entidad, "P17")
    sedes = [q for q in items(entidad, "P159") if q not in paises]
    sede = {}
    if sedes and referencias.get(sedes[0], {}).get("etiqueta"):
        sede["ciudad"] = referencias[sedes[0]]["etiqueta"]
    if paises and referencias.get(paises[0], {}).get("etiqueta"):
        sede["pais"] = referencias[paises[0]]["etiqueta"]
    coordenadas = referencias.get(sedes[0], {}).get("coordenadas") if sedes else None
    if not coordenadas:
        propias = reclamos(entidad, "P625")
        if propias:
            coordenadas = [valor(propias[0])["longitude"], valor(propias[0])["latitude"]]
    if coordenadas:
        sede["ubicacion"] = {"type": "Point", "coordinates": coordenadas}
    return sede


def cantidad(texto):
    """Convierte "+57350000000" o "+1.5" de Wikidata en int o float."""
    numero = Decimal(texto)
    return int(numero) if numero == numero.to_integral_value() else float(numero)


def construir_serie(entidad, propiedad, monedas=None):
    """Serie {anio, valor[, moneda]} con un dato por año (y moneda).
    Descarta datos sin año (calificador P585) y monedas sin código ISO.
    Devuelve (serie, descartados)."""
    elegidos, descartados = {}, 0
    for reclamo in reclamos(entidad, propiedad):
        anio = anio_calificador(reclamo, "P585")
        v = valor(reclamo)
        moneda = None
        if monedas is not None:
            unidad = v.get("unit", "").rsplit("/", 1)[-1]
            moneda = monedas.get(unidad, {}).get("iso4217")
            if not moneda or not re.fullmatch(r"[A-Z]{3}", moneda):
                descartados += 1
                continue
        if anio is None:
            descartados += 1
            continue
        clave = (anio, moneda)
        # reclamos() ya viene con los preferidos primero: el primero gana.
        if clave not in elegidos:
            dato = {"anio": anio, "valor": cantidad(v["amount"])}
            if moneda:
                dato["moneda"] = moneda
            elegidos[clave] = dato
    return sorted(elegidos.values(), key=lambda d: (d["anio"], d.get("moneda", ""))), descartados


def construir_metricas(raw):
    empleados, desc_e = construir_serie(raw["entidad"], "P1128")
    ingresos, desc_i = construir_serie(raw["entidad"], "P2139", raw.get("monedas", {}))
    metricas = {}
    if empleados:
        metricas["empleados"] = empleados
    if ingresos:
        metricas["ingresos"] = ingresos
    return metricas, desc_e + desc_i


def construir_documento(raw, fila, ahora):
    entidad, referencias = raw["entidad"], raw["referencias"]
    fundacion, anios_fundacion = elegir_fecha(entidad, "P571")
    if fundacion is None:
        return None, "sin fecha de fundación en Wikidata"
    lugares = items(entidad, "P740")
    if lugares and referencias.get(lugares[0], {}).get("etiqueta"):
        fundacion["lugar"] = {"ciudad": referencias[lugares[0]]["etiqueta"]}
    disolucion, _ = elegir_fecha(entidad, "P576")

    documento = {
        "_id": raw["_id"],
        "nombre": etiqueta(entidad, fila["nombre"]),
        "sectores": construir_sectores(fila),
        "estado": "disuelta" if disolucion else "activa",
        "fundacion": fundacion,
        "disolucion": disolucion,
        "meta": {"version_esquema": VERSION_ESQUEMA, "fuentes": ["wikidata"],
                 "actualizado_en": ahora},
    }
    sede = construir_sede(entidad, referencias)
    if sede:
        documento["sede"] = sede
    documento["metricas"], documento["_descartados"] = construir_metricas(raw)
    # Uso interno de la parte 3 (alertas): no se guarda.
    documento["_anios_fundacion"] = anios_fundacion
    return documento, None


# ---------- parte 2: relaciones, notas, hitos y estado ----------

def anio_calificador(reclamo, propiedad):
    for calificador in reclamo.get("qualifiers", {}).get(propiedad, []):
        tiempo = calificador.get("datavalue", {}).get("value", {}).get("time", "")
        coincidencia = FORMATO_TIEMPO.match(tiempo)
        if coincidencia:
            return int(coincidencia.group(2))
    return None


def referencia(empresa_id, anio):
    return {"empresa_id": empresa_id, **({"anio": anio} if anio else {})}


def aplicar_wikidata(documento, raw, catalogo):
    """Fundadores y relaciones que Wikidata declara entre empresas del catálogo."""
    entidad, referencias = raw["entidad"], raw["referencias"]
    qid = documento["_id"]

    documento["fundadores"] = [
        {"nombre": referencias[q]["etiqueta"], "qid": q}
        for q in items(entidad, "P112") if referencias.get(q, {}).get("etiqueta")
    ]

    relaciones = {}
    # Matriz o dueño: solo cuenta si es empresa del catálogo. P127 incluye
    # accionistas institucionales (Vanguard, State Street), que quedan fuera.
    for propiedad in ("P749", "P127"):
        for reclamo in reclamos(entidad, propiedad):
            dueno = valor(reclamo).get("id")
            if dueno in catalogo and dueno != qid:
                anio = anio_calificador(reclamo, "P580")
                if anio is None and documento["disolucion"]:
                    anio = documento["disolucion"]["anio"]
                relaciones["adquirida_por"] = referencia(dueno, anio)
                break
        if "adquirida_por" in relaciones:
            break

    antecesores = []
    for propiedad, campo in (("P1365", "predecesor"), ("P1366", "sucesor")):
        for q in items(entidad, propiedad):
            if q in catalogo and q != qid and campo not in relaciones:
                relaciones[campo] = referencia(q, None)
            elif q not in catalogo and propiedad == "P1365":
                nombre = referencias.get(q, {}).get("etiqueta")
                if nombre:
                    antecesores.append(nombre)

    documento["relaciones"] = relaciones
    documento["atributos"] = {"antecesores": antecesores} if antecesores else {}
    documento["nombres_historicos"] = []
    documento["hitos"] = []


def aplicar_notas(documentos, lista_completa, avisos):
    """Aplica las notas de la lista semilla. Tienen prioridad sobre Wikidata."""
    for fila in lista_completa.values():
        entradas = [e.strip() for e in fila.get("notas", "").split("|") if e.strip()]
        for entrada in entradas:
            if fila["decision"] == "incluir" and (m := NOTA_RELACION.match(entrada)):
                tipo, otra, anio = m.group(1), m.group(2), int(m.group(3))
                documento = documentos.get(fila["qid"])
                if documento is None or otra not in documentos:
                    avisos.append(f"nota sin destino en el catálogo: {fila['nombre']}: {entrada}")
                    continue
                relaciones = documento["relaciones"]
                relaciones[CAMPO_RELACION[tipo]] = referencia(otra, anio)
                # Una escisión anotada anula una "matriz" de Wikidata hacia la misma
                # empresa (HPE aparece en Wikidata como filial de HP).
                if tipo == "escindida de":
                    previa = relaciones.get("adquirida_por", {})
                    if previa.get("empresa_id") == otra and not any(
                            e.startswith(f"adquirida por {otra}") for e in entradas):
                        relaciones.pop("adquirida_por")
                if tipo == "fusionada con":
                    documento["_fusionada"] = anio

            elif fila["decision"] == "fusionar" and (m := NOTA_FUSIONAR.match(entrada)):
                destino = documentos.get(m.group(1))
                if destino is None:
                    avisos.append(f"fusionar sin destino: {fila['nombre']}: {entrada}")
                    continue
                historico = {"nombre": fila["nombre"]}
                if fila.get("fundacion", "").isdigit():
                    historico["desde"] = int(fila["fundacion"])
                destino["nombres_historicos"].append(historico)

            elif fila["decision"] == "hito" and (m := NOTA_HITO.match(entrada)):
                matriz, tipo, anio, desde = m.group(1), m.group(2), int(m.group(3)), m.group(4)
                if matriz not in documentos or tipo not in TIPOS_HITO:
                    avisos.append(f"hito no aplicable: {fila['nombre']}: {entrada}")
                    continue
                if desde and desde in documentos:
                    documentos[matriz]["hitos"].append({
                        "anio": anio, "tipo": tipo, "contraparte_id": desde,
                        "descripcion": f"Adquiere {fila['nombre']} de {documentos[desde]['nombre']}"})
                    documentos[desde]["hitos"].append({
                        "anio": anio, "tipo": tipo, "contraparte_id": matriz,
                        "descripcion": f"Vende {fila['nombre']} a {documentos[matriz]['nombre']}"})
                else:
                    textos = {"lanzamiento": f"Lanza {fila['nombre']}"}
                    documentos[matriz]["hitos"].append({
                        "anio": anio, "tipo": tipo,
                        "descripcion": textos.get(tipo, fila["nombre"])})


def derivar_hitos_y_estado(documentos):
    """Convierte las relaciones en hitos de ambas empresas y define el estado final."""
    for documento in documentos.values():
        relaciones = documento["relaciones"]
        compra = relaciones.get("adquirida_por")
        if compra and compra.get("anio"):
            comprador = documentos.get(compra["empresa_id"], {"hitos": []})
            comprador["hitos"].append({
                "anio": compra["anio"], "tipo": "adquisicion",
                "contraparte_id": documento["_id"],
                "descripcion": f"Adquiere {documento['nombre']}"})
        separacion = relaciones.get("escindida_de")
        if separacion and separacion.get("anio"):
            origen = documentos.get(separacion["empresa_id"], {"hitos": []})
            origen["hitos"].append({
                "anio": separacion["anio"], "tipo": "otro",
                "contraparte_id": documento["_id"],
                "descripcion": f"Se separa {documento['nombre']}"})

        # El estado lo define el evento más reciente; una escisión posterior a una
        # compra devuelve la independencia (PayPal: comprada en 2002, separada en 2015).
        eventos = []
        if compra:
            eventos.append((compra.get("anio") or 0, "adquirida"))
        if separacion:
            eventos.append((separacion.get("anio") or 0, "activa"))
        if "_fusionada" in documento:
            eventos.append((documento.pop("_fusionada"), "fusionada"))
        if eventos and not documento.pop("_estado_fijo", False):
            documento["estado"] = max(eventos)[1]
            if documento["estado"] == "activa" and documento["disolucion"]:
                documento["estado"] = "disuelta"

    for documento in documentos.values():
        documento.pop("_estado_fijo", None)
        documento["hitos"].sort(key=lambda h: (h["anio"], h["descripcion"]))


# ---------- parte 4: override y alertas ----------

CAMPOS_PROTEGIDOS = {"_id", "override", "meta"}


def combinar(base, cambios):
    """Objetos se combinan; valores y listas reemplazan; None borra la clave."""
    for clave, nuevo in cambios.items():
        if nuevo is None:
            base.pop(clave, None)
        elif isinstance(nuevo, dict) and isinstance(base.get(clave), dict):
            combinar(base[clave], nuevo)
        else:
            base[clave] = deepcopy(nuevo)


def agregar(base, elementos):
    """Suma elementos a listas existentes (o las crea), a cualquier profundidad."""
    for clave, nuevo in elementos.items():
        if isinstance(nuevo, list):
            base.setdefault(clave, []).extend(deepcopy(nuevo))
        elif isinstance(nuevo, dict):
            agregar(base.setdefault(clave, {}), nuevo)


def aplicar_override(documentos, overrides, avisos):
    for qid, override in overrides.items():
        documento = documentos.get(qid)
        if documento is None:
            avisos.append(f"override de {qid} sin empresa en el catálogo")
            continue
        campos = dict(override.get("campos", {}))
        for clave in CAMPOS_PROTEGIDOS & set(campos):
            avisos.append(f"override de {qid}: el campo {clave} no se puede corregir")
            campos.pop(clave)
        combinar(documento, campos)
        agregar(documento, override.get("agregar", {}))
        if "estado" in campos:
            documento["_estado_fijo"] = True
        documento["meta"]["fuentes"].append("override")
        documento["_override"] = override


def recortar_metricas(documento):
    """Quita datos de métricas anteriores a la fundación (cifras pro forma de antes
    de que la empresa existiera, como HPE antes de 2015). Devuelve cuántos quitó."""
    fundacion = documento["fundacion"]["anio"]
    quitados = 0
    for nombre, serie in list(documento.get("metricas", {}).items()):
        validos = [d for d in serie if d["anio"] >= fundacion]
        quitados += len(serie) - len(validos)
        if validos:
            documento["metricas"][nombre] = validos
        else:
            documento["metricas"].pop(nombre)
    return quitados


def construir_resumen(wikipedia):
    """Resumen en español; si no hay, en inglés. Incluye la URL para la atribución."""
    for idioma in ("es", "en"):
        datos = (wikipedia or {}).get(idioma)
        if datos and datos.get("extracto"):
            return {"texto": datos["extracto"], "idioma": idioma,
                    "fuente": "wikipedia", "url": datos.get("url")}
    return None


def calcular_alertas(documento, anios_fundacion, notas, anios_dbpedia=()):
    alertas = []
    override = documento.get("_override", {})
    corregidas = set(override.get("campos", {}))
    fundacion = documento["fundacion"]["anio"]
    disolucion = (documento.get("disolucion") or {}).get("anio")

    if disolucion and fundacion > disolucion:
        alertas.append({"regla": "fundacion_posterior_disolucion",
                        "detalle": f"fundación {fundacion}, disolución {disolucion}"})

    anios_datos = [h["anio"] for h in documento["hitos"]]
    for serie in documento.get("metricas", {}).values():
        anios_datos += [d["anio"] for d in serie]
    if anios_datos and min(anios_datos) < fundacion:
        alertas.append({"regla": "fundacion_posterior_primer_dato",
                        "detalle": f"fundación {fundacion}, primer dato {min(anios_datos)}"})

    if len(anios_fundacion) > 1 and max(anios_fundacion) - min(anios_fundacion) > 5:
        alertas.append({"regla": "fundaciones_dispersas",
                        "detalle": "años de fundación en Wikidata: "
                                   + ", ".join(map(str, anios_fundacion))})

    if documento["estado"] == "adquirida" and disolucion:
        alertas.append({"regla": "estado_corregido",
                        "detalle": f"disolución {disolucion} tratada como adquisición"})

    if anios_dbpedia and fundacion not in anios_dbpedia:
        alertas.append({"regla": "discrepancia_dbpedia",
                        "detalle": f"fundación {fundacion}, DBpedia: "
                                   + ", ".join(map(str, anios_dbpedia))})

    if not {"fundacion", "disolucion"} & corregidas:
        for entrada in (e.strip() for e in notas.split("|")):
            if entrada.startswith("fecha dudosa:"):
                alertas.append({"regla": "fecha_dudosa",
                                "detalle": entrada.split(":", 1)[1].strip() or "sin detalle"})

    # Una alerta sobre un campo ya corregido por override deja de mostrarse.
    if "fundacion" in corregidas:
        alertas = [a for a in alertas if a["regla"] not in
                   ("fundacion_posterior_disolucion", "fundacion_posterior_primer_dato",
                    "fundaciones_dispersas", "discrepancia_dbpedia")]
    if "disolucion" in corregidas or "estado" in corregidas:
        alertas = [a for a in alertas if a["regla"] != "estado_corregido"]
    return alertas


# ---------- ejecución ----------

def leer_lista():
    with LISTA.open(encoding="utf-8-sig", newline="") as archivo:
        return {f["qid"]: f for f in csv.DictReader(archivo)}


def main():
    load_dotenv()
    bd = MongoClient(os.environ["URI_MONGO"])[os.environ["BD_MONGO"]]
    lista_completa = leer_lista()
    lista = {q: f for q, f in lista_completa.items() if f["decision"] == "incluir"}
    solo = sys.argv[1] if len(sys.argv) > 1 else None
    ahora = datetime.now(timezone.utc)

    # Primera pasada: datos propios de cada empresa (parte 1).
    documentos, raws, omitidas, avisos = {}, {}, [], []
    descartados_metricas, anios_fundacion = 0, {}
    filtro = {"_id": {"$in": list(lista)}}
    wikipedia = {d["_id"]: d for d in bd["raw_wikipedia"].find(filtro)}
    dbpedia = {d["_id"]: d.get("anios_fundacion", []) for d in bd["raw_dbpedia"].find(filtro)}
    for raw in bd["raw_wikidata"].find({"_id": {"$in": list(lista)}}):
        documento, motivo = construir_documento(raw, lista[raw["_id"]], ahora)
        if documento is None:
            omitidas.append(f"{raw['_id']} {raw['nombre_lista']}: {motivo}")
            continue
        anios_fundacion[raw["_id"]] = documento.pop("_anios_fundacion")
        descartados_metricas += documento.pop("_descartados")
        resumen = construir_resumen(wikipedia.get(raw["_id"]))
        if resumen:
            documento["resumen"] = resumen
            documento["meta"]["fuentes"].append("wikipedia")
        if dbpedia.get(raw["_id"]):
            documento["meta"]["fuentes"].append("dbpedia")
        documentos[raw["_id"]] = documento
        raws[raw["_id"]] = raw

    # Segunda pasada: relaciones entre empresas (necesita el catálogo completo).
    catalogo = set(documentos)
    for qid, documento in documentos.items():
        aplicar_wikidata(documento, raws[qid], catalogo)
    aplicar_notas(documentos, lista_completa, avisos)

    # Override: correcciones manuales guardadas en cada ficha.
    overrides = {d["_id"]: d["override"] for d in
                 bd["empresas"].find({"override": {"$exists": True}}, {"override": 1})}
    aplicar_override(documentos, overrides, avisos)
    previos_fundacion = sum(recortar_metricas(d) for d in documentos.values())
    derivar_hitos_y_estado(documentos)

    for qid, documento in documentos.items():
        documento["meta"]["alertas"] = calcular_alertas(
            documento, anios_fundacion[qid], lista[qid].get("notas", ""),
            dbpedia.get(qid, []))

    if solo:
        if solo in documentos:
            vista = {k: v for k, v in documentos[solo].items() if k != "_override"}
            print(json.dumps(vista, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"{solo} no está en el catálogo o no tiene fecha de fundación")
        return

    guardadas, rechazadas = 0, []
    for qid, documento in documentos.items():
        try:
            # Se reemplaza la ficha completa (así desaparecen campos que override
            # borró) y se vuelve a poner el override tal como estaba.
            override = documento.pop("_override", None)
            completo = {**documento, **({"override": override} if override else {})}
            bd["empresas"].replace_one({"_id": qid}, completo, upsert=True)
            guardadas += 1
        except WriteError as error:
            detalle = error.details.get("errInfo", {}).get("details", {})
            rechazadas.append(f"{qid} {documento['nombre']}: "
                              f"{json.dumps(detalle, ensure_ascii=False, default=str)[:300]}")

    estados = {}
    for documento in documentos.values():
        estados[documento["estado"]] = estados.get(documento["estado"], 0) + 1
    print(f"empresas: {guardadas} guardadas de {len(lista)}")
    print("estados:", ", ".join(f"{e} {n}" for e, n in sorted(estados.items())))
    print("con adquirida_por:", sum(1 for d in documentos.values()
                                    if "adquirida_por" in d["relaciones"]))
    print("hitos generados:", sum(len(d["hitos"]) for d in documentos.values()))
    print("con empleados:", sum(1 for d in documentos.values() if "empleados" in d["metricas"]),
          "| con ingresos:", sum(1 for d in documentos.values() if "ingresos" in d["metricas"]),
          "| datos descartados (sin año o sin moneda ISO):", descartados_metricas)
    monedas = {}
    for d in documentos.values():
        for dato in d["metricas"].get("ingresos", []):
            monedas[dato["moneda"]] = monedas.get(dato["moneda"], 0) + 1
    print("monedas en ingresos:", ", ".join(f"{m} {n}" for m, n in sorted(monedas.items())))
    print("datos de métricas anteriores a la fundación quitados:", previos_fundacion)
    print("con resumen:", sum(1 for d in documentos.values() if "resumen" in d),
          "| en español:", sum(1 for d in documentos.values()
                               if d.get("resumen", {}).get("idioma") == "es"))
    print("fichas con override:", len(overrides))
    reglas = {}
    for d in documentos.values():
        for alerta in d["meta"]["alertas"]:
            reglas[alerta["regla"]] = reglas.get(alerta["regla"], 0) + 1
    print("alertas:", ", ".join(f"{r} {n}" for r, n in sorted(reglas.items())) or "ninguna",
          "| fichas con alertas:", sum(1 for d in documentos.values() if d["meta"]["alertas"]))
    for linea in omitidas:
        print("OMITIDA:", linea)
    for linea in avisos:
        print("AVISO:", linea)
    for linea in rechazadas:
        print("RECHAZADA POR EL VALIDADOR:", linea)


if __name__ == "__main__":
    main()
