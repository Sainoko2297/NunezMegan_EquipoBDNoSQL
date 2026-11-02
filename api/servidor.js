import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import { conectar, desconectar, obtenerBd } from './bd.js';
import rutasEmpresas from './rutas/empresas.js';
import rutasEcosistema from './rutas/ecosistema.js';

const variablesRequeridas = ['URI_MONGODB', 'BASE_DATOS', 'PUERTO', 'ORIGEN_INTERFAZ'];
const faltantes = variablesRequeridas.filter((nombre) => !process.env[nombre]);
if (faltantes.length > 0) {
  console.error(`Faltan variables en .env: ${faltantes.join(', ')}`);
  process.exit(1);
}

const app = express();
app.use(helmet());
app.use(cors({ origin: process.env.ORIGEN_INTERFAZ }));

app.get('/api/salud', async (req, res) => {
  await obtenerBd().command({ ping: 1 });
  res.json({ estado: 'ok', base_datos: 'conectada' });
});

app.use('/api/empresas', rutasEmpresas);
app.use('/api/ecosistema', rutasEcosistema);

app.use((req, res) => {
  res.status(404).json({ error: 'Ruta no encontrada' });
});

app.use((error, req, res, next) => {
  console.error(error);
  res.status(500).json({ error: 'Error interno del servidor' });
});

try {
  await conectar();
} catch (error) {
  console.error('No se pudo conectar a MongoDB:', error.message);
  process.exit(1);
}

const puerto = Number(process.env.PUERTO);
const servidor = app.listen(puerto, '127.0.0.1', () => {
  console.log(`API escuchando en http://127.0.0.1:${puerto}`);
});

async function cerrar() {
  servidor.close();
  await desconectar();
  process.exit(0);
}

process.on('SIGINT', cerrar);
process.on('SIGTERM', cerrar);