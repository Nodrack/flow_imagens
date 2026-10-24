import io
from pathlib import Path
import numpy as np
import pytest
from PIL import Image, ImageFilter
from flowapp.quality import QualityRules, inspect_image
from flowapp.schemas import ExportRules
from flowapp.images import ImageStore, ImageValidationError


def test_blur_noise_exposure_and_resolution(tmp_path):
    rng = np.random.default_rng(42)
    image = Image.fromarray(rng.integers(50, 205, (256, 256), dtype=np.uint8))
    path = tmp_path / "image.png"
    image.save(path)
    sharp = inspect_image(path, QualityRules(), ExportRules())
    image.filter(ImageFilter.GaussianBlur(5)).save(path)
    blurred = inspect_image(path, QualityRules(), ExportRules())
    assert sharp['metrics']['sharpness'] > blurred['metrics']['sharpness']
    assert sharp['metrics']['noise'] > blurred['metrics']['noise']
    assert any('Nitidez' in r for r in blurred['reasons'])
    for color, key in [(0, 'black_percent'), (255, 'white_percent')]:
        Image.new('L', (50, 40), color).save(path)
        report = inspect_image(path, QualityRules(), ExportRules(min_megapixels=4))
        assert report['metrics'][key] == 100
        assert report['status'] == 'blocked'
    path.write_bytes(b'broken')
    assert inspect_image(path, QualityRules(), ExportRules())['status'] == 'blocked'


def test_export_preview_confirmation_and_size_block(tmp_path):
    class DB:
        def update_asset_export(self, *args):
            pass
    source = tmp_path / 'source.png'
    Image.new('RGB', (50, 40), 'gray').save(source)
    asset = dict(path=str(source), id='asset', collection_id='collection', sha256='hash')
    store = ImageStore(tmp_path, DB())
    rules = ExportRules(technical_rules_verified=True)
    _, preview = store.export_jpeg(asset, 'Title', ['test'], rules, quality_rules=QualityRules(), preview=True)
    assert preview['technical']['status'] == 'warning'
    assert not (tmp_path / 'collections').exists()
    with pytest.raises(ImageValidationError, match='Confirme'):
        store.export_jpeg(asset, 'Title', ['test'], rules, quality_rules=QualityRules())
    output, report = store.export_jpeg(asset, 'Title', ['test'], rules, quality_rules=QualityRules(), allow_warnings=True)
    assert output.exists() and report['technical']['status'] == 'warning'
    with pytest.raises(ImageValidationError, match='bloqueada'):
        store.export_jpeg(asset, 'Title', ['test'], ExportRules(max_file_bytes=10), quality_rules=QualityRules(), allow_warnings=True)
