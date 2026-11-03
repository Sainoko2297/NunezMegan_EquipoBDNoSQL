import { MongoClient } from 'mongodb';

let cliente = null;
let baseDatos = null;

export async function conectar() {
  cliente = new MongoClient(process.env.URI_MONGODB, {
    serverSelectionTimeoutMS: 5000,
  });
  await cliente.connect();
  baseDatos = cliente.db(process.env.BASE_DATOS);
  await baseDatos.command({ ping: 1 });
}

export function obtenerBd() {
  if (!baseDatos) {
    throw new Error('La base de datos no está conectada');
  }
  return baseDatos;
}

export async function desconectar() {
  await cliente?.close();
}