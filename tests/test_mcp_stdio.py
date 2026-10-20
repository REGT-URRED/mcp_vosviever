"""Prueba de humo del servidor por el protocolo MCP real (stdio).

No usa el cliente de OpenCode: levanta el servidor como lo haria cualquier
cliente MCP, hace el handshake, lista las herramientas y ejecuta una corrida
real de VOSviewer. Sirve para detectar dos cosas que un test unitario no ve:
que el servidor arranque y que las tools respondan.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVIDOR = os.path.join(RAIZ, "vosviewer_mcp.py")

VOSVIEWER_DISPONIBLE = os.path.isfile(
    os.environ.get(
        "VOSVIEWER_EXE", r"C:\Users\TOOR\Downloads\actividad\hoy\VOSviewer.exe"
    )
)


def _mapa(path):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("id\tlabel\tdescription\tx\ty\tcluster\tweight<Links>\n")
        for i, (x, y) in enumerate([(0.0, 0.0), (1.0, 1.5), (-2.0, 0.5), (3.0, -1.0)], 1):
            fh.write("%d\tterm%d\t\t%.3f\t%.3f\t%d\t%d\n" % (i, i, x, y, i, i))
    return path


def _red(path):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        for a, b, w in [(1, 2, 3), (2, 3, 1), (3, 4, 2)]:
            fh.write("%d\t%d\t%d\n" % (a, b, w))
    return path


async def _con_sesion(fn):
    """Abre el servidor, hace el handshake y ejecuta fn con la sesion viva."""
    parametros = StdioServerParameters(
        command=sys.executable, args=[SERVIDOR], env=dict(os.environ)
    )
    async with stdio_client(parametros) as (lectura, escritura):
        async with ClientSession(lectura, escritura) as sesion:
            await sesion.initialize()
            return await fn(sesion)


def test_el_servidor_arranca_y_publica_las_diez_tools():
    async def correr(sesion):
        respuesta = await sesion.list_tools()
        return [t.name for t in respuesta.tools]

    nombres = asyncio.run(_con_sesion(correr))
    assert len(nombres) == 10, nombres
    assert "vos_analyze" in nombres
    assert "vos_capabilities" in nombres


def test_una_tool_que_no_toca_vosviewer_responde():
    async def correr(sesion):
        return await sesion.call_tool("vos_capabilities", {"section": "save"})

    resultado = asyncio.run(_con_sesion(correr))
    assert not resultado.is_error
    assert "save_map" in str(resultado.content[0].text)


@pytest.mark.skipif(not VOSVIEWER_DISPONIBLE, reason="VOSviewer.exe no esta instalado")
def test_corrida_real_escribe_el_archivo(tmp_path):
    """La regresion del 'Acceso denegado', vista desde el protocolo real."""
    mapa = _mapa(str(tmp_path / "mapa.txt"))
    red = _red(str(tmp_path / "red.txt"))
    destino = str(tmp_path / "salida" / "mapa_out.txt")

    async def correr(sesion):
        return await sesion.call_tool(
            "vos_analyze",
            {
                "source": "map",
                "source_path": mapa,
                "network_path": red,
                "layout": {"run_layout": True, "run_clustering": True, "min_cluster_size": 1},
                "visualization": {"network_visualization": True},
                "outputs": {"save_map": destino},
                "timeout_s": 120,
            },
        )

    resultado = asyncio.run(_con_sesion(correr))
    assert not resultado.is_error, resultado
    assert os.path.isfile(destino)
    assert os.path.getsize(destino) > 0

