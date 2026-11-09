"""The warm start's model lookup must not re-point the model manager.

Seen on a restart: the first launch selected the 1B (``last_model.json``
written), the second launch's warm start called
``resolve_model_path(data_root, id)``, which created the process-wide model
manager on ``<data root>/models``. In a source checkout the models live in
``<repo>/models`` (``services.paths.models_dir()``), so the warm start found
nothing and, for the whole session, ``GET /api/models`` showed every model as
not downloaded and ``POST /api/models/select`` answered 404.
"""
from __future__ import annotations

from services import model_manager as mm
from services.config import resolve_model_path


def test_resolve_uses_the_models_dir_not_the_data_root(tmp_path, monkeypatch):
    models = tmp_path / "checkout-models"
    models.mkdir()
    info = mm.RECOMMENDED_MODELS["llama-3.2-1b-q4"]
    (models / info.filename).write_bytes(b"GGUF")
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv("IMURUN_MODELS_DIR", str(models))
    monkeypatch.setattr(mm, "_model_manager", None)

    assert resolve_model_path(data_root, "llama-3.2-1b-q4") == str(models / info.filename)
    # ...and the manager every route uses afterwards still sees the model.
    assert mm.get_model_manager().models_dir == models
    assert mm.get_model_manager().get_model_path("llama-3.2-1b-q4") == models / info.filename
