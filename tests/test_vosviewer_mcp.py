"""Tests del servidor MCP de VOSviewer.

El foco esta en lo que el servidor tiene que garantizar por si mismo, sin
lanzar VOSviewer: preparar las carpetas de salida, avisar cuando un archivo
queda vacio y rechazar entradas que VOSviewer va a rechazar con un dialogo
modal opaco.

El caso que mas duele esta en `test_analyze_escribe_en_carpeta_nueva`: el
proceso javaw.exe de VOSviewer corre con Low Mandatory Level y no puede
escribir en una carpeta de integridad media. Java lo reporta como
"Acceso denegado" en un dialogo que el usuario tiene que cerrar a mano.
"""
from __future__ import annotations

import os
import subprocess

import pytest

import vosviewer_mcp as v


VOSVIEWER_DISPONIBLE = os.path.isfile(v.VOSVIEWER_EXE)


# ---------------------------------------------------------------- utilerios

def _escribir_mapa(path, coordenadas=None, cluster_iguales=False):
    """Mapa en el formato de VOSviewer: tab-delimitado con cabecera."""
    coords = coordenadas or [(0.0, 0.0), (1.0, 1.5), (-2.0, 0.5), (3.0, -1.0)]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("id\tlabel\tdescription\tx\ty\tcluster\tweight<Links>\n")
        for i, (x, y) in enumerate(coords, 1):
            cluster = 1 if cluster_iguales else i
            fh.write("%d\tterm%d\t\t%.3f\t%.3f\t%d\t%d\n" % (i, i, x, y, cluster, i))
    return path


def _escribir_red(path, separador="\t"):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        for a, b, w in [(1, 2, 3), (2, 3, 1), (3, 4, 2)]:
            fh.write(separador.join(str(x) for x in (a, b, w)) + "\n")
    return path


def _etiqueta_integridad(path):
    try:
        out = subprocess.run(
            ["icacls", path], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(out.split())


# ---------------------------------------------- preparacion de carpetas

def test_prepare_out_dir_crea_el_padre_que_falta(tmp_path):
    destino = tmp_path / "nuevo" / "anidado" / "mapa.txt"
    assert not destino.parent.exists()

    v._prepare_out_dir(str(destino))

    assert destino.parent.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="la etiqueta de integridad es propia de Windows")
def test_prepare_out_dir_deja_la_carpeta_escribible_para_vosviewer(tmp_path):
    """VOSviewer corre con integridad baja: sin esta etiqueta no puede escribir.

    Sin esto el usuario ve un dialogo modal 'Acceso denegado' y tiene que
    cerrarlo a mano en cada corrida.
    """
    destino = tmp_path / "salida" / "mapa.txt"

    v._prepare_out_dir(str(destino))

    etiqueta = _etiqueta_integridad(str(destino.parent))
    assert "Low Mandatory Level" in etiqueta, (
        "la carpeta de salida no quedo con etiqueta Low Mandatory Level: %r" % etiqueta
    )


# ---------------------------------------------- deteccion de salidas vacias

def test_collect_outputs_marca_el_archivo_vacio(tmp_path):
    vacio = tmp_path / "captura.png"
    vacio.write_bytes(b"")
    con_datos = tmp_path / "mapa.txt"
    con_datos.write_text("id\tlabel\n", encoding="utf-8")

    reporte = v._collect_outputs({"save_screenshot_png": str(vacio), "save_map": str(con_datos)})

    assert reporte["save_screenshot_png"]["empty"] is True
    assert reporte["save_screenshot_png"]["bytes"] == 0
    assert "empty" not in reporte["save_map"]


def test_collect_outputs_marca_el_archivo_ausente(tmp_path):
    reporte = v._collect_outputs({"save_map": str(tmp_path / "no-existe.txt")})

    assert reporte["save_map"]["missing"] is True


# ---------------------------------------------- validacion de entradas

def test_validate_map_rechaza_coordenadas_identicas(tmp_path):
    """Es exactamente el error que VOSviewer devuelve en un dialogo modal opaco."""
    mapa = _escribir_mapa(tmp_path / "mapa.txt", coordenadas=[(0.0, 0.0)] * 4, cluster_iguales=True)

    problemas = v._validate_map_file(str(mapa))

    assert problemas, "un mapa con las mismas coordenadas tiene que rechazarse"
    texto = " ".join(problemas).lower()
    assert "coordenadas" in texto
    assert "same coordinates" in texto or "coinciden" in texto


def test_validate_map_acepta_un_mapa_valido(tmp_path):
    mapa = _escribir_mapa(tmp_path / "mapa.txt")

    assert v._validate_map_file(str(mapa)) == []


def test_validate_map_avisa_si_falta_el_archivo(tmp_path):
    problemas = v._validate_map_file(str(tmp_path / "no-existe.txt"))

    assert problemas
    assert "no existe" in " ".join(problemas).lower()


def test_validate_network_rechaza_separado_por_espacios(tmp_path):
    """La red debe ir tab-delimitada: separada por espacios VOSviewer no la lee."""
    red = _escribir_red(tmp_path / "red.txt", separador=" ")

    problemas = v._validate_network_file(str(red))

    assert problemas
    assert "tabulador" in " ".join(problemas).lower()


def test_validate_network_acepta_red_tab_delimitada(tmp_path):
    red = _escribir_red(tmp_path / "red.txt")

    assert v._validate_network_file(str(red)) == []


# ---------------------------------------------- integracion real

@pytest.mark.skipif(not VOSVIEWER_DISPONIBLE, reason="VOSviewer.exe no esta instalado")
def test_analyze_escribe_en_carpeta_nueva(tmp_path):
    """Regresion del 'Acceso denegado': la carpeta destino se crea y se escribe."""
    mapa = _escribir_mapa(tmp_path / "mapa.txt")
    red = _escribir_red(tmp_path / "red.txt")
    destino = tmp_path / "salida" / "reciprocal" / "mapa_out.txt"

    resultado = v.vos_analyze(
        source="map",
        source_path=str(mapa),
        network_path=str(red),
        layout={"run_layout": True, "run_clustering": True, "min_cluster_size": 1},
        visualization={"network_visualization": True},
        outputs={"save_map": str(destino)},
        timeout_s=120,
    )

    assert resultado["ok"], resultado
    assert destino.is_file(), "VOSviewer no escribio el mapa de salida"
    assert destino.stat().st_size > 0


@pytest.mark.skipif(not VOSVIEWER_DISPONIBLE, reason="VOSviewer.exe no esta instalado")
def test_analyze_recupera_el_screenshot_que_vosviewer_dejo_vacio(tmp_path):
    """En batch, -save_screenshot_png deja un PNG de 0 bytes.

    El servidor tiene que entregar una imagen de verdad: si no, el que recibe
    el archivo vazio tiene que saber que esta ahi el problema.
    """
    mapa = _escribir_mapa(tmp_path / "mapa.txt")
    red = _escribir_red(tmp_path / "red.txt")
    salida = tmp_path / "salida"

    resultado = v.vos_analyze(
        source="map",
        source_path=str(mapa),
        network_path=str(red),
        layout={"run_layout": True, "run_clustering": True, "min_cluster_size": 1},
        visualization={"network_visualization": True},
        outputs={
            "save_map": str(salida / "mapa_out.txt"),
            "save_screenshot_png": str(salida / "01_red.png"),
        },
        timeout_s=120,
    )

    assert resultado["ok"], resultado
    captura = salida / "01_red.png"
    assert captura.is_file(), "no se escribio la captura"
    assert captura.stat().st_size > 0, "la captura quedo vacia"


def test_analyze_falla_rapido_si_ya_hay_una_instancia_viva(tmp_path, monkeypatch):
    """Con la aplicacion abierta, la corrida batch se queda colgada esperando."""
    mapa = _escribir_mapa(tmp_path / "mapa.txt")
    red = _escribir_red(tmp_path / "red.txt")
    monkeypatch.setattr(v, "_alive_pids", lambda: [4242])

    resultado = v.vos_analyze(
        source="map",
        source_path=str(mapa),
        network_path=str(red),
        layout={"run_layout": True, "run_clustering": True, "min_cluster_size": 1},
        visualization={"network_visualization": True},
        outputs={"save_map": str(tmp_path / "salida" / "mapa_out.txt")},
        timeout_s=30,
    )

    assert not resultado["ok"]
    assert "instancia" in str(resultado.get("error", "")).lower()
