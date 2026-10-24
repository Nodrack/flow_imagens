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



def test_regional_metrics_ignore_flat_copy_space_and_detect_blur(tmp_path):
    from flowapp.quality import regional_metrics
    rules = QualityRules(region_size=64, region_min_contrast=2)
    flat = regional_metrics(Image.new('L', (128, 128), 100), rules)
    assert flat == {'analyzed_regions': 0, 'local_sharpness_p25': None, 'local_detail_ratio': None}
    rng = np.random.default_rng(2026)
    image = Image.fromarray(rng.integers(20, 235, (256, 256), dtype=np.uint8))
    sharp = regional_metrics(image, rules)
    blurred = regional_metrics(image.filter(ImageFilter.GaussianBlur(2)), rules)
    assert sharp['analyzed_regions'] == 16
    assert sharp['local_sharpness_p25'] > blurred['local_sharpness_p25']
    assert sharp['local_detail_ratio'] > blurred['local_detail_ratio']
    path = tmp_path / 'noisy.png'
    image.save(path)
    report = inspect_image(path, rules, ExportRules())
    assert any('alta frequência' in reason for reason in report['reasons'])


def test_regional_parameters_roundtrip_config(tmp_path):
    from flowapp.config import AppConfig, save_config, load_config
    config = AppConfig(quality_rules={'region_size': 64, 'region_min_contrast': 3,
                                     'min_local_sharpness_p25': 5, 'max_local_detail_ratio': 2})
    path = tmp_path / 'config.json'
    save_config(path, config)
    assert load_config(path).quality_rules == config.quality_rules
    with pytest.raises(ValueError):
        QualityRules(region_size=2)
