"""`L1.17` — the capital that may actually be risked, measured and bounded.

The claim under test is a shape, not a number: **the answer is `min(what the broker has, what the
operator permits)`, and when the broker cannot be read there is no answer at all.** Falling back to
the ceiling would substitute a permission for a measurement, which is how a system sizes a trade it
cannot fund.

The real payload in `test_the_real_account_shape_is_pinned` is the one this account actually
returned on 2026-08-13 with the market open — equity `net` of minus 88 rupees, cash zero. An
account in debit is an ordinary measurement, and the only unacceptable answer for it is a
tradeable one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from nse_algo_trader.capital_configuration import TradingCapital
from nse_algo_trader.deployable_capital_resolver import (
    EQUITY_SEGMENT,
    BrokerBalanceUnreadableError,
    DeployableCapitalStore,
    KiteMarginsBalanceSource,
    SegmentBalance,
    resolve_deployable_capital,
    segment_balances_from_margins_payload,
)

pytestmark = pytest.mark.unit

_AT = datetime(2026, 8, 13, 11, 55, tzinfo=UTC)
_CEILING = TradingCapital.of_rupees(Decimal("1000000"))


class _Source:
    def __init__(self, *balances: SegmentBalance) -> None:
        self._balances = balances

    @property
    def source_name(self) -> str:
        return "fake"

    def segment_balances(self) -> tuple[SegmentBalance, ...]:
        return self._balances


def _segment(
    segment: str = EQUITY_SEGMENT, net: str = "500000", cash: str = "500000"
) -> SegmentBalance:
    return SegmentBalance(
        segment=segment,
        net_rupees=Decimal(net),
        cash_rupees=Decimal(cash),
        collateral_rupees=Decimal(0),
        live_balance_rupees=Decimal(net),
        enabled=True,
    )


class TestTheAnswerIsTheTighterOfTheTwoBounds:
    def test_the_broker_binds_when_it_holds_less_than_the_ceiling(self) -> None:
        capital = resolve_deployable_capital(_CEILING, _Source(_segment()), measured_at=_AT)
        assert capital.deployable_rupees == Decimal("500000")
        assert capital.binding_side == "broker_balance"
        assert capital.is_tradeable

    def test_the_ceiling_binds_when_the_broker_holds_more(self) -> None:
        capital = resolve_deployable_capital(
            _CEILING, _Source(_segment(net="2500000", cash="2500000")), measured_at=_AT
        )
        assert capital.deployable_rupees == Decimal("1000000")
        assert capital.binding_side == "operator_ceiling"

    def test_a_deposit_cannot_raise_the_deployable_figure_on_its_own(self) -> None:
        """The property the ceiling exists for: more money in the account is not more permission."""
        before = resolve_deployable_capital(_CEILING, _Source(_segment()), measured_at=_AT)
        after = resolve_deployable_capital(
            _CEILING, _Source(_segment(net="9900000", cash="9900000")), measured_at=_AT
        )
        assert after.deployable_rupees == _CEILING.total_rupees
        assert after.deployable_rupees >= before.deployable_rupees
        assert after.binding_side == "operator_ceiling"

    def test_a_withdrawal_lowers_it_with_no_act_from_anyone(self) -> None:
        capital = resolve_deployable_capital(
            _CEILING, _Source(_segment(net="1200", cash="1200")), measured_at=_AT
        )
        assert capital.deployable_rupees == Decimal("1200")


class TestADebitIsAMeasurementNotAnError:
    def test_a_negative_balance_deploys_nothing_and_says_which_bound(self) -> None:
        """The real account on 2026-08-13. The only unacceptable answer here is a tradeable one."""
        capital = resolve_deployable_capital(
            _CEILING, _Source(_segment(net="-88", cash="0")), measured_at=_AT
        )
        assert capital.deployable_rupees == Decimal(0)
        assert not capital.is_tradeable
        assert capital.binding_side == "broker_balance_exhausted"

    def test_a_debit_is_not_netted_against_another_segment(self) -> None:
        """Commodity money cannot fund an equity order this system is not permitted to place."""
        capital = resolve_deployable_capital(
            _CEILING,
            _Source(_segment(net="-88", cash="0"), _segment(segment="commodity", net="750000")),
            measured_at=_AT,
        )
        assert capital.deployable_rupees == Decimal(0)


class TestItRefusesRatherThanSubstitutingTheCeiling:
    def test_an_unreadable_broker_is_a_refusal(self) -> None:
        class Unreadable:
            @property
            def source_name(self) -> str:
                return "unreadable"

            def segment_balances(self) -> tuple[SegmentBalance, ...]:
                raise BrokerBalanceUnreadableError("the broker did not answer")

        with pytest.raises(BrokerBalanceUnreadableError):
            resolve_deployable_capital(_CEILING, Unreadable(), measured_at=_AT)

    def test_no_segments_at_all_is_not_zero_in_each(self) -> None:
        with pytest.raises(BrokerBalanceUnreadableError, match="no segments"):
            resolve_deployable_capital(_CEILING, _Source(), measured_at=_AT)

    def test_a_segment_this_system_sizes_against_must_be_measured(self) -> None:
        with pytest.raises(BrokerBalanceUnreadableError, match="said nothing about"):
            resolve_deployable_capital(
                _CEILING, _Source(_segment(segment="commodity")), measured_at=_AT
            )


class TestThePayloadIsNormalisedOrRefused:
    def test_the_real_account_shape_is_pinned(self) -> None:
        """Read off the live account on 2026-08-13, not from documentation."""
        payload: dict[str, Any] = {
            "equity": {
                "enabled": True,
                "net": -88,
                "segment": "equity",
                "available": {
                    "adhoc_margin": 0,
                    "cash": 0,
                    "collateral": 0,
                    "intraday_payin": 0,
                    "live_balance": -88,
                    "opening_balance": 0,
                },
                "utilised": {"debits": 88},
            },
            "commodity": {
                "enabled": True,
                "net": 0,
                "available": {
                    "adhoc_margin": 0,
                    "cash": 0,
                    "collateral": 0,
                    "intraday_payin": 0,
                    "live_balance": 0,
                    "opening_balance": 0,
                },
                "utilised": {},
            },
        }
        balances = segment_balances_from_margins_payload(payload)
        assert [balance.segment for balance in balances] == ["commodity", "equity"]
        equity = balances[1]
        assert equity.net_rupees == Decimal("-88")
        assert equity.deployable_rupees == Decimal(0)

    @pytest.mark.adversarial
    @pytest.mark.parametrize(
        "payload",
        [None, [], "no", {}, {"equity": None}, {"equity": {"net": 1}}],
    )
    def test_an_answer_this_module_cannot_read_is_never_a_balance_of_zero(
        self, payload: Any
    ) -> None:
        with pytest.raises(BrokerBalanceUnreadableError):
            segment_balances_from_margins_payload(payload)

    @pytest.mark.adversarial
    def test_a_float_becomes_an_exact_decimal_through_its_string(self) -> None:
        """Kite hands back binary floats; `Decimal(2811.35)` is not 2811.35."""
        payload = {
            "equity": {
                "enabled": True,
                "net": 2811.35,
                "available": {"cash": 2811.35, "collateral": 0, "live_balance": 2811.35},
            }
        }
        assert segment_balances_from_margins_payload(payload)[0].net_rupees == Decimal("2811.35")

    @pytest.mark.adversarial
    def test_a_boolean_is_not_an_amount(self) -> None:
        payload = {
            "equity": {
                "enabled": True,
                "net": True,
                "available": {"cash": 0, "collateral": 0, "live_balance": 0},
            }
        }
        with pytest.raises(BrokerBalanceUnreadableError, match="boolean"):
            segment_balances_from_margins_payload(payload)

    @pytest.mark.adversarial
    def test_the_live_client_refusing_becomes_a_refusal_and_not_an_empty_answer(self) -> None:
        class AngryClient:
            def margins(self) -> dict[str, Any]:
                raise TimeoutError("read timed out")

        with pytest.raises(BrokerBalanceUnreadableError, match="could not be read"):
            KiteMarginsBalanceSource(AngryClient()).segment_balances()


class TestCollateralIsNotCash:
    def test_pledged_collateral_does_not_raise_the_deployable_figure(self) -> None:
        """The broker counts collateral toward margin; this does not.

        Pledged securities carry their own haircut and cannot pay for a loss, so counting them is
        how a margin call arrives on a day the market has already moved.
        """
        pledged = SegmentBalance(
            segment=EQUITY_SEGMENT,
            net_rupees=Decimal("1000"),
            cash_rupees=Decimal("1000"),
            collateral_rupees=Decimal("400000"),
            live_balance_rupees=Decimal("1000"),
            enabled=True,
        )
        capital = resolve_deployable_capital(_CEILING, _Source(pledged), measured_at=_AT)
        assert capital.deployable_rupees == Decimal("1000")
        assert capital.segments[0].collateral_rupees == Decimal("400000"), (
            "the exclusion must stay visible, not silent"
        )


class TestTheMeasurementIsKept:
    def test_an_observation_can_be_read_back_whole(self, tmp_path: Path) -> None:
        capital = resolve_deployable_capital(
            _CEILING, _Source(_segment(net="-88", cash="0")), measured_at=_AT
        )
        with DeployableCapitalStore(tmp_path / "deployable_capital.sqlite3") as store:
            store.record(capital, detail="daily run")
            latest = store.latest()
            assert latest is not None
            assert latest.deployable_rupees == Decimal(0)
            assert latest.binding_side == "broker_balance_exhausted"
            assert [balance.segment for balance in latest.segments] == [EQUITY_SEGMENT]
            assert store.observation_count() == 1

    def test_an_empty_store_has_no_latest_rather_than_a_zero(self, tmp_path: Path) -> None:
        with DeployableCapitalStore(tmp_path / "deployable_capital.sqlite3") as store:
            assert store.latest() is None
