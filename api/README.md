# NunezMegan_EquipoBDNoSQL
Repositorio para subir archivos del equipo de Base de Datos No SQL

# API del catálogo de empresas

API de solo lectura en Node.js y Express sobre la colección `empresas` de MongoDB. La colección la construye y actualiza el worker de `ingesta`; la API nunca escribe.

## Requisitos

- Node.js 20.6 o superior (usa `--env-file`).
- MongoDB con la colección `empresas` cargada (ver `ingesta/guia_de_ejecucion.txt`).

## Instalación

```
cd api
npm install
```

Copiar `.env.example` como `.env` y ajustar los valores:

| Variable | Descripción |
|---|---|
| URI_MONGODB | Cadena de conexión a MongoDB |
| BASE_DATOS | Base donde el worker creó la colección `empresas` |
| PUERTO | Puerto local de la API |
| ORIGEN_INTERFAZ | Único origen permitido por CORS |

## Ejecución

```
npm run desarrollo    # reinicia al guardar cambios
npm run iniciar       # sin reinicio automático
```

## Endpoints

| Método y ruta | Descripción |
|---|---|
| GET /api/salud | Comprueba la conexión con MongoDB |
| GET /api/empresas | Lista paginada. Parámetros: `pagina`, `limite` (1 a 100), `sector`, `buscar` |
| GET /api/empresas/:qid | Ficha completa. Incluye `corregida: true` si tiene correcciones manuales |
| GET /api/empresas/:qid/genealogia | Compradores, compras y escisiones en cadena (`$graphLookup`, hasta 10 niveles) |
| GET /api/ecosistema/:anio | Empresas activas, nacidas, adquiridas, fusionadas y desaparecidas en un año (1947 al actual) |

`:qid` es el identificador de Wikidata, por ejemplo `Q37156` (IBM).

Sectores válidos: `computacion`, `semiconductores`, `software`, `internet`, `telecomunicaciones`, `servicios_ti`.

## Reglas del ecosistema por año

- **activas**: operaban de forma independiente. Dejan de contar desde el año de su compra, fusión o disolución, y vuelven a contar si una escisión posterior les devuelve la independencia.
- **nacidas**: fundadas en el año.
- **adquiridas**: compradas en el año, con su comprador.
- **fusionadas**: fusionadas en el año, con su sucesor.
- **desaparecidas**: disueltas en el año sin comprador.

## Errores

Todas las respuestas de error son JSON con la forma `{ "error": "mensaje" }`: 400 para parámetros inválidos, 404 para recursos inexistentes y 500 para errores internos, sin detalles del servidor.

## Limitaciones conocidas

- `buscar` encuentra palabras completas, no fragmentos (`micro` no encuentra Microsoft).
- La genealogía sigue un solo tipo de relación por recorrido, y se detiene en compradores fuera del catálogo.
- Las fechas se guardan en UTC; al mostrarlas, formatear con `timeZone: 'UTC'`.
- El servidor escucha solo en `127.0.0.1`. Antes de desplegar hay que hacer configurable la dirección y activar autenticación en MongoDB.