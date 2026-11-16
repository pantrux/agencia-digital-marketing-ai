"""Adaptadores para probar el constructor con archivos temporales sin root."""

import os
import stat
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def local_owner_as_root(normal):
    checked_node = normal.checked_node

    def check_fixture_node(info, uid, *, directory=False):
        if uid == 0 and (info.st_uid, info.st_gid) == (os.geteuid(), os.getegid()):
            # Conserva el modo, tipo y número de enlaces que devuelve el sistema de archivos.
            fields = list(info)
            fields[stat.ST_UID] = fields[stat.ST_GID] = 0
            info = os.stat_result(fields)
        return checked_node(info, uid, directory=directory)

    with patch.object(normal, "checked_node", check_fixture_node):
        yield
