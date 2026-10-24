import json

import pytest

from flowapp.briefings import (
    BriefingTemplate, CommercialBriefings, concept_reference, template_widget_values,
    templates_from_rows, templates_rows,
)
from flowapp.config import AppConfig, load_config, save_config
from flowapp.schemas import CollectionBrief


def test_default_templates_are_valid_editable_collection_briefs():
    settings = CommercialBriefings()
    assert {t.label for t in settings.templates} == {
        'Ingredientes e comida', 'Fundos com espaço para texto', 'Negócios com uso claro',
        'Tecnologia com uso claro', 'Ambientes',
    }
    for template in settings.templates:
        brief = CollectionBrief(**template.brief_values())
        assert brief.audience and brief.commercial_application and brief.composition_constraints
    assert templates_from_rows(templates_rows(settings)) == settings


def test_template_application_is_limited_to_brief_fields_and_manual_orientation_stays_independent():
    template = CommercialBriefings().templates[0]
    values = template_widget_values(template, 'new_brief')
    assert values['new_brief_theme'] == template.theme
    assert values['new_brief_orientation'] == template.orientation
    assert values['new_brief_constraints'] == '; '.join(template.composition_constraints)
    assert not any(key.endswith(('_name', '_count', '_catalog', '_sales', '_prompt')) for key in values)
    manual = template_widget_values(template, 'manual_brief', include_orientation=False)
    assert 'manual_brief_orientation' not in manual


def test_invalid_duplicate_templates_and_partial_rows_are_rejected():
    template = CommercialBriefings().templates[0]
    with pytest.raises(ValueError, match='nome diferente'):
        CommercialBriefings(templates=[template, template.model_copy(update={'label': template.label.upper()})])
    with pytest.raises(ValueError):
        templates_from_rows([{'Modelo': 'Incompleto'}])
    assert templates_from_rows([{}]).templates == []
    with pytest.raises(ValueError):
        BriefingTemplate(label=' ', theme='Tema', objective='Objetivo')


def test_template_settings_and_visual_limits_persist_without_touching_calendar(tmp_path):
    settings = CommercialBriefings()
    settings.templates[0].objective = 'Objetivo personalizado para uma campanha'
    config = AppConfig(commercial_briefings=settings, visual_reference_limit=3, visual_reference_prompt_chars=50)
    path = tmp_path / 'config.json'
    save_config(path, config)
    assert load_config(path) == config
    assert load_config(path).seasonal_calendar.enabled


def test_visual_reference_preserves_composition_and_limits_prompt_text():
    plan = {'concept': 'Cozinha clara', 'main_subject': 'Mesa com frutas',
            'composition_and_copy_space': 'Vista superior, frutas à direita, texto à esquerda',
            'prompt_en': 'A white kitchen with orange fruit, overhead view. ' * 100,
            'commercial_application': 'Banner de alimentos', 'orientation': 'horizontal'}
    reference = json.loads(concept_reference(plan, 50))
    assert reference['composition_and_copy_space'] == plan['composition_and_copy_space']
    assert reference['prompt_en'] == plan['prompt_en'][:50]
    assert reference['commercial_application'] == 'Banner de alimentos'
