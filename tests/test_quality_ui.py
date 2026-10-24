"""Exercise the export screen with isolated files; never touches production data."""
from pathlib import Path
from streamlit.testing.v1 import AppTest

HARNESS = r'''
import streamlit as st
from pathlib import Path
from types import SimpleNamespace
import app
from flowapp.config import AppConfig
from flowapp.images import ImageStore
from flowapp.orchestrator import Orchestrator
from PIL import Image

if "orch" not in st.session_state:
    root = Path(ROOT_PATH)
    path = root / "source.png"
    Image.new("RGB", (96, 64), "gray").save(path)
    concept = {"id": "concept", "state": "metadata_ready", "data": {"concept": "Imagem de teste"}}
    asset = {"id": "asset", "collection_id": "collection", "concept_id": "concept", "role": "upscaled",
             "path": str(path), "sha256": "hash", "width": 96, "height": 64,
             "metadata": {"title_en": "Test image", "keywords": ["test"]}}
    class DB:
        def list_assets(self, *args): return [asset]
        def update_asset_export(self, asset_id, report): asset["export"] = report
        def transition_concept(self, cid, state, reason): concept["state"] = state.value
    config = AppConfig(quality_rules={"min_sharpness": 0} if APPROVED else {}, export_rules={"technical_rules_verified": True, "max_file_bytes": 10 if BLOCKED else None})
    orch = Orchestrator.__new__(Orchestrator)
    orch.database = DB()
    orch.config = config
    orch.images = ImageStore(root, orch.database)
    st.session_state.orch = orch
    st.session_state.concept = concept
app.render_concept_export(st.session_state.orch, "collection", st.session_state.concept)
'''


def screen(tmp_path, blocked=False, approved=False):
    source = HARNESS.replace("ROOT_PATH", repr(str(tmp_path))).replace("BLOCKED", repr(blocked)).replace("APPROVED", repr(approved))
    at = AppTest.from_string(source).run(timeout=20)
    assert not at.exception
    return at


def test_warning_requires_confirmation_then_prepares_jpeg(tmp_path):
    at = screen(tmp_path)
    assert at.warning
    assert at.button[0].disabled
    assert not list(tmp_path.rglob("*.jpg"))
    at.checkbox[0].check().run()
    assert not at.exception and not at.button[0].disabled
    at.button[0].click().run()
    assert not at.exception
    assert len(list(tmp_path.rglob("*.jpg"))) == 1
    assert at.session_state.concept["state"] == "prepared_for_transfer"
    assert at.session_state.orch.database.list_assets()[0]["export"]["technical"]["status"] == "warning"


def test_file_size_block_cannot_be_overridden_in_screen(tmp_path):
    at = screen(tmp_path, blocked=True)
    at.checkbox[0].check().run()
    assert not at.exception
    assert at.button[0].disabled
    assert not list(tmp_path.rglob("*.jpg"))



def test_approved_image_can_be_prepared_without_warning_checkbox(tmp_path):
    at = screen(tmp_path, approved=True)
    assert not at.warning and not at.checkbox
    assert not at.button[0].disabled
    at.button[0].click().run()
    assert not at.exception
    assert at.session_state.orch.database.list_assets()[0]['export']['technical']['status'] == 'approved'
