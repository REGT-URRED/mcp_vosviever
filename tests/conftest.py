import os
import sys

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import vosviewer_mcp as v


def _candidatos_java8():
    """Runtime de Java 8 para las pruebas de integracion.

    Se busca en el entorno y despues en la ubicacion convencional de esta
    maquina. Si no hay ninguno, las pruebas que lanzan VOSviewer se omiten.
    """
    vistos = []
    for nombre in ("VOSVIEWER_JAVA_HOME", "JAVA_HOME"):
        valor = os.environ.get(nombre)
        if valor:
            vistos.append(valor)
    vistos.append(
        os.path.join(os.path.expanduser("~"), ".config", "opencode", "java8")
    )
    for candidato in vistos:
        version = v._java_version(candidato)
        # Java 8 se reporta como 1.8
        if version and (version == (1, 8) or version[0] == 8):
            return candidato
    return None


JAVA8 = _candidatos_java8()
if JAVA8:
    os.environ["VOSVIEWER_JAVA_HOME"] = JAVA8


@pytest.fixture(autouse=True)
def _cerrar_vosviewer():
    """Ningun test debe dejar VOSviewer vivo: una instancia huerfana hace que
    la corrida siguiente se cuelgue esperando un dialogo que no aparece."""
    yield
    try:
        v.vos_close()
    except Exception:
        pass
