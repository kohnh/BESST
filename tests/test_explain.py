from besst.battery import Battery
from besst.policy import hindsight
from tests.test_optimizer import sine_prices


def test_every_action_has_a_reason_naming_the_price():
    df = hindsight(sine_prices(), Battery())
    active = df[df.action != "idle"]
    assert len(active) > 0
    for row in active.itertuples():
        assert row.reason and f"${row.rrp:,.0f}/MWh" in row.reason
    assert df.reason.str.len().gt(0).all()
