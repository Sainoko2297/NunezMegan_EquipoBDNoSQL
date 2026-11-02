import { Router } from 'express';
import { obtenerBd } from '../bd.js';

const rutas = Router();
const patronQid = /^Q[0-9]+$/;

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

export default rutas;