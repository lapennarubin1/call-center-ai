#!/usr/bin/env python3
"""
Crea (o actualiza) el usuario administrador del panel.
Uso:
    python3 crear_admin.py <usuario> <contraseña>
"""
import sys
from app import app
from models import db, AdminUser

if len(sys.argv) != 3:
    sys.exit("Uso: python3 crear_admin.py <usuario> <contraseña>")

usuario, clave = sys.argv[1], sys.argv[2]

with app.app_context():
    u = AdminUser.query.filter_by(username=usuario).first()
    if not u:
        u = AdminUser(username=usuario)
        db.session.add(u)
    u.set_password(clave)
    db.session.commit()
    print(f"Usuario '{usuario}' guardado.")
