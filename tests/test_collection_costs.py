from datetime import date, timedelta

import pytest

from flowapp.database import Database
from flowapp.metrics import revenue_per_hour, result_after_costs
from flowapp.moderation import today_local
from flowapp.schemas import CollectionBrief, CollectionCostEntry, MonthlySalesEntry, PerformanceEntry


@pytest.fixture
def costs_env(tmp_path):
    db = Database(tmp_path / 'flow.sqlite3')
    cid = db.create_collection('GPU finalistas', CollectionBrief(theme='Teste', objective='Medir retorno'))
    return db, cid


def gross(db):
    return revenue_per_hour(db.performance_comparison(), db.work_minutes_by_collection(),
                            db.list_monthly_sales(), db.list_archived_work())


def test_cost_validation_and_persistence(costs_env):
    db, cid = costs_env
    entry = CollectionCostEntry(cost_date='2026-10-01', amount=1.234567, currency='USD', notes='GPU alugada')
    cost_id = db.add_collection_cost(cid, entry)
    saved = Database(db.path).list_collection_costs(cid)[0]
    assert saved['id'] == cost_id and saved['amount_micros'] == 1_234_567
    assert saved['amount'] == 1.234567 and saved['notes'] == 'GPU alugada'
    for invalid in [dict(amount=0), dict(amount=-1), dict(amount=float('nan')), dict(amount=float('inf')),
                    dict(currency='usd'), dict(cost_date=today_local()+timedelta(days=1))]:
        with pytest.raises(ValueError):
            CollectionCostEntry(**({'cost_date': date(2026, 10, 1), 'amount': 1, 'currency': 'USD'} | invalid))
    with pytest.raises(KeyError):
        db.add_collection_cost('missing', entry)


def test_cost_only_negative_result_and_zero_hours_without_currency_conversion(costs_env):
    db, cid = costs_env
    db.add_collection_cost(cid, CollectionCostEntry(cost_date='2026-10-01', amount=5, currency='EUR'))
    result = result_after_costs(gross(db), db.costs_for_metrics())
    assert result['collections'][cid]['by_currency']['EUR'] == {
        'revenue': 0, 'cost': 5, 'result': -5, 'per_hour': None}
    assert result['total']['by_currency']['EUR']['result'] == -5


def test_legacy_costs_and_manual_entries_count_once_and_survive_collection_removal(costs_env):
    db, cid = costs_env
    db.add_work_session(cid, '2026-10-01', 60, 'Produção')
    db.record_performance(cid, PerformanceEntry(report_date='2026-10-01', revenue=10, revenue_currency='USD',
                                              manual_minutes=30, user_cost=2, cost_currency='EUR'))
    db.upsert_monthly_sales(cid, '2026-10', downloads=2, revenue=20, currency='USD')
    cost_id = db.add_collection_cost(cid, CollectionCostEntry(cost_date='2026-10-01', amount=1, currency='USD'))
    revenue = gross(db)
    before = result_after_costs(revenue, db.costs_for_metrics())
    assert revenue['collections'][cid]['by_currency']['USD']['per_hour'] == 20
    assert before['collections'][cid]['by_currency']['USD']['result'] == 29
    assert before['collections'][cid]['by_currency']['USD']['per_hour'] == pytest.approx(29/1.5)
    assert before['collections'][cid]['by_currency']['EUR']['result'] == -2
    assert before['collections'][cid]['by_currency']['EUR']['per_hour'] == pytest.approx(-2/1.5)
    db.delete_collection_records(cid)
    after = result_after_costs(gross(db), db.costs_for_metrics())
    assert after['total'] == before['total']
    archived = db.list_collection_costs()[0]
    assert archived['id'] == cost_id and archived['collection_id'] is None and archived['detached_from'] == cid
    assert after['collections'][f'removida:{cid}'] == before['collections'][cid]


def test_delete_is_scoped_and_explicit(costs_env):
    db, cid = costs_env
    other = db.create_collection('Outra', CollectionBrief(theme='Teste', objective='Outra coleção'))
    cost_id = db.add_collection_cost(cid, CollectionCostEntry(cost_date='2026-10-01', amount=3, currency='BRL'))
    with pytest.raises(KeyError):
        db.delete_collection_cost(cost_id, other)
    assert len(db.list_collection_costs(cid)) == 1
    db.delete_collection_cost(cost_id, cid)
    assert not db.list_collection_costs(cid)


def test_global_result_uses_all_work_hours_without_summing_currencies(costs_env):
    db, cid = costs_env
    other = db.create_collection('Outra', CollectionBrief(theme='Teste', objective='Outra coleção'))
    db.add_work_session(cid, '2026-10-01', 60)
    db.add_work_session(other, '2026-10-01', 180)
    db.record_performance(cid, PerformanceEntry(report_date='2026-10-01', revenue=10))
    db.add_collection_cost(other, CollectionCostEntry(cost_date='2026-10-01', amount=2, currency='USD'))
    db.add_collection_cost(cid, CollectionCostEntry(cost_date='2026-10-01', amount=3, currency='EUR'))
    result = result_after_costs(gross(db), db.costs_for_metrics())
    assert result['total']['hours'] == 4
    assert result['total']['by_currency']['USD']['per_hour'] == 2
    assert result['total']['by_currency']['EUR']['per_hour'] == -0.75

