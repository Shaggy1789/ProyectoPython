"""
Configuración central de la base de datos.

Expone una instancia de SQLAlchemy no ligada que se inicializa con la
aplicación Flask (app.py). Separada en su propio módulo para evitar
importaciones circulares entre app, modelos y autenticación.
"""

import os

from flask_sqlalchemy import SQLAlchemy

from dotenv import load_dotenv

load_dotenv()

db = SQLAlchemy()


def configure_database(app):
    """Configura SQLAlchemy sobre la aplicación Flask."""
    database_url = os.getenv('DATABASE_URL')
    if database_url and database_url.startswith('postgres://'):
        # Normalizar prefijo para SQLAlchemy 2.x (requiere postgresql://)
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)