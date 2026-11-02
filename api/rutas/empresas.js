import { Router } from 'express';
import { obtenerBd } from '../bd.js';

const rutas = Router();
const patronQid = /^Q[0-9]+$/;
const SECTORES = [
  'computacion',
  'semiconductores',
  'software',
  'internet',
  'telecomunicaciones',
  'servicios_ti',
];
const LIMITE_POR_DEFECTO = 20;
const LIMITE_MAXIMO = 100;
const LARGO_MAXIMO_BUSQUEDA = 100;

const PROFUNDIDAD_MAXIMA = 10;

rutas.param('qid', (req, res, next, qid) => {
  if (!patronQid.test(qid)) {
    return res.status(400).json({
      error: 'El identificador debe ser Q seguido de números, por ejemplo Q37156',
    });
  }
  next();
});

function resumirNodos(campo, relacion) {
  return {
    $sortArray: {
      input: {
        $map: {
          input: `$${campo}`,
          as: 'nodo',
          in: {
            _id: '$$nodo._id',
            nombre: '$$nodo.nombre',
            estado: '$$nodo.estado',
            nivel: { $add: ['$$nodo.nivel', 1] },
            vinculo: `$$nodo.relaciones.${relacion}`,
          },
        },
      },
      sortBy: { nivel: 1, nombre: 1 },
    },
  };
}

function leerEntero(valor, porDefecto) {
  if (valor === undefined) return porDefecto;
  if (typeof valor !== 'string' || !/^[0-9]+$/.test(valor)) return null;
  return Number(valor);
}

rutas.get('/', async (req, res) => {
  const pagina = leerEntero(req.query.pagina, 1);
  const limite = leerEntero(req.query.limite, LIMITE_POR_DEFECTO);
  const { sector, buscar } = req.query;

  if (pagina === null || pagina < 1) {
    return res.status(400).json({ error: 'pagina debe ser un entero mayor o igual a 1' });
  }
  if (limite === null || limite < 1 || limite > LIMITE_MAXIMO) {
    return res.status(400).json({ error: `limite debe ser un entero entre 1 y ${LIMITE_MAXIMO}` });
  }
  if (sector !== undefined && (typeof sector !== 'string' || !SECTORES.includes(sector))) {
    return res.status(400).json({ error: `sector debe ser uno de: ${SECTORES.join(', ')}` });
  }
  if (
    buscar !== undefined &&
    (typeof buscar !== 'string' || buscar.trim() === '' || buscar.length > LARGO_MAXIMO_BUSQUEDA)
  ) {
    return res.status(400).json({
      error: `buscar debe ser un texto de 1 a ${LARGO_MAXIMO_BUSQUEDA} caracteres`,
    });
  }

  const filtro = {};
  if (sector) filtro.sectores = sector;
  if (buscar) filtro.$text = { $search: buscar.trim() };

  const proyeccion = {
    nombre: 1,
    sectores: 1,
    estado: 1,
    'fundacion.anio': 1,
    'sede.ciudad': 1,
    'sede.pais': 1,
  };
  const orden = buscar ? { relevancia: { $meta: 'textScore' }, nombre: 1 } : { nombre: 1 };

  const coleccion = obtenerBd().collection('empresas');
  let cursor = coleccion
    .find(filtro, { projection: proyeccion })
    .sort(orden)
    .skip((pagina - 1) * limite)
    .limit(limite);
  if (!buscar) {
    cursor = cursor.collation({ locale: 'es', strength: 1 });
  }

  const [datos, total] = await Promise.all([cursor.toArray(), coleccion.countDocuments(filtro)]);

  res.json({
    datos,
    paginacion: { pagina, limite, total, paginas: Math.ceil(total / limite) },
  });
});

rutas.get('/:qid', async (req, res) => {
  const { qid } = req.params;

  if (!patronQid.test(qid)) {
    return res.status(400).json({
      error: 'El identificador debe ser Q seguido de números, por ejemplo Q37156',
    });
  }

  const [empresa] = await obtenerBd()
    .collection('empresas')
    .aggregate([
      { $match: { _id: qid } },
      { $set: { corregida: { $eq: [{ $type: '$override' }, 'object'] } } },
      { $unset: ['override', 'meta.alertas'] },
    ])
    .toArray();

  if (!empresa) {
    return res.status(404).json({ error: 'Empresa no encontrada' });
  }

  res.json(empresa);
});

rutas.get('/:qid/genealogia', async (req, res) => {
  const { qid } = req.params;
  const recorrido = (como, inicio, desde, hacia) => ({
    $graphLookup: {
      from: 'empresas',
      startWith: inicio,
      connectFromField: desde,
      connectToField: hacia,
      as: como,
      maxDepth: PROFUNDIDAD_MAXIMA,
      depthField: 'nivel',
    },
  });

  const [resultado] = await obtenerBd()
    .collection('empresas')
    .aggregate([
      { $match: { _id: qid } },
      recorrido('compradores', '$relaciones.adquirida_por.empresa_id',
        'relaciones.adquirida_por.empresa_id', '_id'),
      recorrido('compras', '$_id', '_id', 'relaciones.adquirida_por.empresa_id'),
      recorrido('escisiones', '$_id', '_id', 'relaciones.escindida_de.empresa_id'),
      {
        $project: {
          nombre: 1,
          compradores: resumirNodos('compradores', 'adquirida_por'),
          compras: resumirNodos('compras', 'adquirida_por'),
          escisiones: resumirNodos('escisiones', 'escindida_de'),
        },
      },
    ])
    .toArray();

  if (!resultado) {
    return res.status(404).json({ error: 'Empresa no encontrada' });
  }

  res.json(resultado);
});

export default rutas;