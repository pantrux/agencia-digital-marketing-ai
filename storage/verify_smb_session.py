"""Verifica JSON de smbstatus por stdin; no conecta ni modifica el NAS o Windows.

Esquema: https://www.samba.org/samba/docs/current/man-html/smbstatus.1.html
La captura debe proceder del comando filtrado por usuario, sin --fast, con salida
cero y sin truncado. Este análisis no acredita el mapping P: ni la prueba triple.
"""

import datetime
import json
import re
import sys

MAX_INPUT = 1024 * 1024
USER = "jandrade"
CLIENT = "192.168.0.6"
SHARE = "producciones"
DIALECTS = {"SMB3_00", "SMB3_02", "SMB3_11"}
CIPHERS = {"AES-128-CCM", "AES-128-GCM", "AES-256-CCM", "AES-256-GCM"}


def identifier(value):
    """Normaliza identificadores numéricos sin aceptar booleanos o ambigüedad."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Identidad de conexión no verificable.")
    text = str(value)
    if not re.fullmatch(r"0|[1-9][0-9]{0,19}", text):
        raise ValueError("Identidad de conexión no verificable.")
    return text


def server_identity(value):
    if not isinstance(value, dict):
        raise ValueError("Falta identidad del proceso Samba.")
    return tuple(identifier(value.get(key)) for key in ("pid", "task_id", "vnn", "unique_id"))


def verify(data):
    """Acredita exclusivamente conexiones completas del recurso y cliente fijos."""
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), dict) for key in ("sessions", "tcons")
    ):
        raise ValueError("Faltan las secciones sessions/tcons del esquema Samba.")
    timestamp = data.get("timestamp")
    version = data.get("version")
    if not isinstance(timestamp, str) or not isinstance(version, str):
        raise ValueError("Faltan fecha y versión de la captura.")
    try:
        observed = datetime.datetime.fromisoformat(timestamp)
    except ValueError:
        raise ValueError("La fecha de captura no es verificable.") from None
    if observed.tzinfo is None or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+){2}(?:[-+][\w.-]+)?", version
    ):
        raise ValueError("La fecha o versión de captura no es verificable.")
    sessions, dialects, ciphers = set(), set(), set()
    connections = 0
    for key, connection in data["tcons"].items():
        if not isinstance(connection, dict):
            raise ValueError("La tabla de recursos no es verificable.")
        if connection.get("service") != SHARE:
            continue
        session_id = identifier(connection.get("session_id"))
        session = data["sessions"].get(session_id)
        if not isinstance(session, dict):
            if connection.get("machine") == CLIENT:
                raise ValueError("El recurso no tiene una sesión correlacionable.")
            continue
        if session.get("username") != USER:
            continue
        if CLIENT not in (connection.get("machine"), session.get("remote_machine")):
            continue
        if connection.get("machine") != CLIENT or session.get("remote_machine") != CLIENT:
            raise ValueError("La IP del recurso y de la sesión no coincide.")
        if identifier(session.get("session_id")) != session_id or identifier(key) != identifier(
            connection.get("tcon_id")
        ):
            raise ValueError("Las claves de sesión o recurso no coinciden.")
        if server_identity(session.get("server_id")) != server_identity(
            connection.get("server_id")
        ):
            raise ValueError("El recurso y la sesión pertenecen a procesos distintos.")
        uid, dialect = session.get("uid"), session.get("session_dialect")
        if (
            isinstance(uid, bool)
            or not isinstance(uid, int)
            or uid < 0
            or not isinstance(dialect, str)
            or dialect not in DIALECTS
        ):
            raise ValueError("La sesión no acredita usuario autenticado y dialecto SMB3.")
        encryption = connection.get("encryption")
        if (
            not isinstance(encryption, dict)
            or encryption.get("degree") != "full"
            or not isinstance(encryption.get("cipher"), str)
            or encryption.get("cipher") not in CIPHERS
        ):
            raise ValueError("El recurso no acredita cifrado SMB completo.")
        sessions.add(session_id)
        dialects.add(dialect)
        ciphers.add(encryption["cipher"])
        connections += 1
    if not connections:
        raise ValueError("No hay conexión acreditable del usuario, IP y recurso esperados.")
    return {
        "verificado": True,
        "fecha_muestra_utc": observed.astimezone(datetime.UTC).isoformat(),
        "version_samba": version,
        "usuario": USER,
        "ip_cliente": CLIENT,
        "recurso": SHARE,
        "cifrado_completo": True,
        "dialectos": sorted(dialects),
        "cifrados": sorted(ciphers),
        "sesiones_verificadas": len(sessions),
        "conexiones_verificadas": connections,
        "limite": "Solo la captura suministrada; no acredita mapping P: ni prueba triple.",
    }


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("El JSON contiene claves duplicadas.")
        result[key] = value
    return result


def main():
    try:
        payload = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(payload) > MAX_INPUT:
            raise ValueError("Límite de entrada excedido.")
        data = json.loads(payload, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, OSError, RecursionError):
        print(json.dumps({"verificado": False, "codigo": "ENTRADA_INVALIDA"}))
        return 2
    try:
        result = verify(data)
    except (ValueError, TypeError, OverflowError):
        print(json.dumps({"verificado": False, "codigo": "SESION_NO_ACREDITADA"}))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
