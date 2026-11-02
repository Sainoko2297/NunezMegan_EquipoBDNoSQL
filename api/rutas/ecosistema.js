import { Router } from 'express';
import { obtenerBd } from '../bd.js';

const rutas = Router();
const ANIO_MINIMO = 1947;
const FUTURO = 9999;
const tarjeta = { _id: 1, nombre: 1, sectores: 1 };

rutas.get('/:anio', async (req, res) => {
  const anioMaximo = new Date().getUTCFullYear();
  const anio = /^[0-9]{4}$/.test(req.params.anio) ? Number(req.params.anio) : null;
  if (anio === null || anio < ANIO_MINIMO || anio > anioMaximo) {
    return res.status(400).json({ error: `El año debe estar entre ${ANIO_MINIMO} y ${anioMaximo}` });
  }

  const [foto] = await obtenerBd()
    .collection('empresas')
    .aggregate(
      [
        { $match: { 'fundacion.anio': { $lte: anio } } },
        {
          $project: {
            nombre: 1,
            sectores: 1,
            estado: 1,
            comprador: '$relaciones.adquirida_por',
            sucesor: '$relaciones.sucesor',
            fundada: '$fundacion.anio',
            compra: { $ifNull: ['$relaciones.adquirida_por.anio', FUTURO] },
            separacion: { $ifNull: ['$relaciones.escindida_de.anio', FUTURO] },
            fin: {
              $cond: [
                { $eq: ['$estado', 'fusionada'] },
                { $ifNull: ['$relaciones.sucesor.anio', '$disolucion.anio', FUTURO] },
                { $ifNull: ['$disolucion.anio', FUTURO] },
              ],
            },
          },
        },
        {
          $set: {
            absorbida: {
              $and: [
                { $lte: ['$compra', anio] },
                { $not: [{ $and: [{ $gt: ['$separacion', '$compra'] }, { $lte: ['$separacion', anio] }] }] },
              ],
            },
          },
        },
        { $sort: { nombre: 1 } },
        {
          $facet: {
            activas: [{ $match: { absorbida: false, fin: { $gt: anio } } }, { $project: tarjeta }],
            nacidas: [{ $match: { fundada: anio } }, { $project: tarjeta }],
            adquiridas: [{ $match: { compra: anio } }, { $project: { ...tarjeta, comprador: 1 } }],
            fusionadas: [{ $match: { estado: 'fusionada', fin: anio } }, { $project: { ...tarjeta, sucesor: 1 } }],
            desaparecidas: [{ $match: { estado: 'disuelta', fin: anio } }, { $project: tarjeta }],
          },
        },
      ],
      { collation: { locale: 'es', strength: 1 } },
    )
    .toArray();

  const resumen = Object.fromEntries(Object.entries(foto).map(([nombre, lista]) => [nombre, lista.length]));
  res.json({ anio, resumen, ...foto });
});

export default rutas;