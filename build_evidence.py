"""Scopus CSV -> redes VOSviewer -> capturas.

Envoltura del servidor MCP: toda la logica (construccion del corpus, la red, las
capturas y el dialogo con VOSviewer) vive en `vosviewer_mcp.py`, que es el
codigo con pruebas. Este script solo aporta la linea de comandos.

Antes este archivo reimplementaba el pipeline completo con otros formatos de
salida y cerraba la aplicacion con `taskkill /IM javaw.exe /F`, que mata
cualquier Java de la maquina. Eso ya no existe.

Uso:
    python build_evidence.py <scopus.csv> <carpeta_de_salida> [analisis ...]

    analisis: coauthorship | authors_sources | title_terms  (por defecto, los tres)
"""
from __future__ import annotations

import json
import os
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import vosviewer_mcp as v

ANALISIS = ("coauthorship", "authors_sources", "title_terms")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    csv_path = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    pedidos = argv[3:] or list(ANALISIS)
    desconocidos = [a for a in pedidos if a not in ANALISIS]
    if desconocidos:
        print("analisis desconocidos: %s" % ", ".join(desconocidos))
        return 2
    if not os.path.isfile(csv_path):
        print("no existe el CSV: %s" % csv_path)
        return 2

    resumen = {}
    for analisis in pedidos:
        resultado = v.vos_from_scopus(
            csv_path=csv_path, analysis=analisis, out_dir=out_dir
        )
        resumen[analisis] = {
            "ok": resultado.get("ok"),
            "folder": resultado.get("folder"),
            "error": resultado.get("error"),
            "stats": resultado.get("stats"),
            "screenshots": [
                {"file": s.get("file"), "bytes": s.get("bytes")}
                for s in resultado.get("screenshots", [])
            ],
        }
        print("%-16s %s" % (analisis, resumen[analisis]["folder"] or resumen[analisis]["error"]))

    print(json.dumps(resumen, ensure_ascii=False, indent=2))
    return 0 if all(r["ok"] for r in resumen.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
