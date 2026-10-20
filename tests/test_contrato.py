"""Contrato de exito y de entrada del servidor.

Estas pruebas no lanzan VOSviewer: fijan el comportamiento que el servidor
promete, para que un cambio futuro no vuelva a reportar exito cuando la
aplicacion fallo.
"""

from __future__ import annotations

import pytest

import vosviewer_mcp as v


def _mapa(path):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("id\tlabel\tdescription\tx\ty\tcluster\tweight<Links>\n")
        for i, (x, y) in enumerate([(0.0, 0.0), (1.0, 1.5), (-2.0, 0.5)], 1):
            fh.write("%d\tterm%d\t\t%.3f\t%.3f\t%d\t%d\n" % (i, i, x, y, i, i))
    return str(path)


def _red(path):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("1\t2\t3\n")
    return str(path)


def _corrida(monkeypatch, tmp_path, **resultado):
    """Ejecuta vos_analyze con una corrida simulada."""
    base = {
        "ok": True,
        "exit_code": 0,
        "elapsed_s": 1.0,
        "stdout": "",
        "stderr": "",
        "killed_children": [],
        "dialogs_answered": [],
        "progress_windows": [],
        "preparation_problems": [],
    }
    base.update(resultado)
    monkeypatch.setattr(v, "_run_batch", lambda argv, timeout_s: dict(base))
    return v.vos_analyze(
        source="map",
        source_path=_mapa(tmp_path / "mapa.txt"),
        network_path=_red(tmp_path / "red.txt"),
        layout={"run_layout": True, "run_clustering": True},
        outputs={"save_map": str(tmp_path / "salida" / "mapa_out.txt")},
        timeout_s=5,
    )


# ------------------------------------------------------------------ exito real

def test_una_corrida_fallida_no_se_reporta_como_exito(monkeypatch, tmp_path):
    """El exito se decide por el codigo de salida, no por la buena voluntad."""
    resultado = _corrida(
        monkeypatch,
        tmp_path,
        ok=False,
        exit_code=1,
        stderr="java.awt.IllegalComponentStateException: component must be showing",
    )

    assert not resultado["ok"]
    assert resultado["exit_code"] == 1
    assert "IllegalComponentState" in resultado["hint"]


def test_una_corrida_fallida_enumera_las_salidas_faltantes(monkeypatch, tmp_path):
    resultado = _corrida(monkeypatch, tmp_path, ok=False, exit_code=1)

    assert resultado["missing_outputs"] == ["save_map"]


def test_una_corrida_exitosa_sin_escribir_no_es_exito(monkeypatch, tmp_path):
    """Codigo 0 sin escribir lo pedido no se puede reportar como exito."""
    resultado = _corrida(monkeypatch, tmp_path)

    assert resultado["ok"] is False
    assert resultado["missing_outputs"] == ["save_map"]
    assert "no escribio" in resultado["hint"]


# ------------------------------------------------------------------ entradas

def test_una_entrada_definida_dos_veces_se_rechaza(tmp_path):
    """source='network' mas network_path no puede分析ar dos archivos distintos."""
    with pytest.raises(ValueError):
        v._build_inputs("network", "a.txt", network_path="b.txt")


def test_las_entradas_inexistentes_se_reportan_antes_de_correr(tmp_path):
    problemas = v._validate_inputs({"map": str(tmp_path / "no-existe.txt")})

    assert problemas
    assert "no existe" in problemas[0]


def test_un_mapa_valido_no_produce_problemas(tmp_path):
    problemas = v._validate_inputs({"map": _mapa(tmp_path / "mapa.txt")})

    assert problemas == []


# ------------------------------------------------------------------ java

def test_el_entorno_usa_el_java_indicado(monkeypatch, tmp_path):
    java_home = tmp_path / "java8"
    (java_home / "bin").mkdir(parents=True)
    monkeypatch.setenv("VOSVIEWER_JAVA_HOME", str(java_home))
    monkeypatch.setenv("PATH", "")

    env = v._vosviewer_env()

    assert env["JAVA_HOME"] == str(java_home)
    assert env["PATH"].startswith(str(java_home / "bin"))


def test_avisa_cuando_el_runtime_no_es_java_8(monkeypatch):
    monkeypatch.setattr(v, "_java_version", lambda java_home=None: (17, 0))

    problema = v._java_problem()

    assert "Java 8" in problema
    assert "VOSVIEWER_JAVA_HOME" in problema


def test_no_avisa_con_java_8(monkeypatch):
    monkeypatch.setattr(v, "_java_version", lambda java_home=None: (1, 8))

    assert v._java_problem() == ""


# ------------------------------------------------------------------ imagenes

def test_una_imagen_lisa_no_cuenta_como_contenido(tmp_path):
    from PIL import Image

    lisa = tmp_path / "lisa.png"
    Image.new("RGB", (40, 40), (10, 10, 10)).save(lisa)
    con = Image.new("RGB", (40, 40), (10, 10, 10))
    con.putpixel((5, 5), (250, 250, 250))
    pintada = tmp_path / "pintada.png"
    con.save(pintada)

    assert v._imagen_con_contenido(str(lisa)) is False
    assert v._imagen_con_contenido(str(pintada)) is True


# ------------------------------------------------------------------ estadisticas

def test_las_estadisticas_ignoran_los_archivos_vacios(tmp_path):
    vacio = tmp_path / "vacio.txt"
    vacio.write_text("", encoding="utf-8")
    bueno = tmp_path / "mapa.txt"
    bueno.write_text("id\tlabel\tx\ty\n1\tuno\t0.0\t1.0\n", encoding="utf-8")

    stats = v._stats_from(
        {"save_map": {"path": str(bueno)}, "save_json": {"path": str(vacio)}}
    )

    assert "items" in stats
    assert stats["items"] == 1
