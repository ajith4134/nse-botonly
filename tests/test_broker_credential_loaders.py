"""`L3.12` — the two credential loaders, and the ways a missing secret can look present.

These are the only modules allowed to know credential environment-variable names, so a
defect here is not a wrong value: it is the whole system reading a DIFFERENT variable than
the operator set, and reporting the broker as unconfigured. Every test below is one way
that mistake can be made silently.
"""

from __future__ import annotations

import pytest

from nse_algo_trader.broker_credentials.broker_api_credentials_loader import (
    _ENV_VAR_PREFIX_BY_BROKER,
    BrokerApiCredentials,
    BrokerName,
    MissingBrokerCredentialsError,
    load_broker_api_credentials,
)
from nse_algo_trader.broker_credentials.kite_login_credentials_loader import (
    KiteLoginCredentials,
    load_kite_login_credentials,
)

# --------------------------------------------------------------- API credentials


@pytest.mark.unit
def test_api_credentials_load_from_an_injected_environment() -> None:
    credentials = load_broker_api_credentials(
        BrokerName.ZERODHA_KITE,
        {"ZERODHA_KITE_API_KEY": "key-1", "ZERODHA_KITE_API_SECRET": "secret-1"},
    )
    assert credentials == BrokerApiCredentials(
        broker_name=BrokerName.ZERODHA_KITE, api_key="key-1", api_secret="secret-1"
    )


@pytest.mark.unit
def test_an_absent_secret_is_none_not_an_error() -> None:
    """A data-only broker has an API key and no secret; that is a valid configuration."""
    credentials = load_broker_api_credentials(BrokerName.GROWW, {"GROWW_API_KEY": "key-only"})
    assert credentials.api_secret is None


@pytest.mark.unit
def test_every_broker_in_the_enum_has_an_environment_prefix() -> None:
    """Adding a broker without a prefix would raise `KeyError` at the first live call.

    The failure would surface inside whatever component first asked for that broker,
    which is a long way from the line that caused it.
    """
    missing = [broker for broker in BrokerName if broker not in _ENV_VAR_PREFIX_BY_BROKER]
    assert missing == [], f"brokers with no env-var prefix: {missing}"


@pytest.mark.adversarial
@pytest.mark.parametrize(
    "api_key_value", ["", "   ", "\t", "\n"], ids=["empty", "spaces", "tab", "newline"]
)
def test_a_blank_api_key_is_missing_not_present(api_key_value: str) -> None:
    """`.env` files acquire trailing whitespace; a key of spaces must not authenticate."""
    with pytest.raises(MissingBrokerCredentialsError, match="ZERODHA_KITE_API_KEY"):
        load_broker_api_credentials(
            BrokerName.ZERODHA_KITE, {"ZERODHA_KITE_API_KEY": api_key_value}
        )


@pytest.mark.adversarial
def test_the_error_names_the_variable_the_operator_must_set() -> None:
    """The whole value of a named error: the operator does not have to read the source."""
    with pytest.raises(MissingBrokerCredentialsError) as failure:
        load_broker_api_credentials(BrokerName.ANGEL_ONE, {})
    assert "ANGEL_ONE_API_KEY" in str(failure.value)
    assert ".env" in str(failure.value)


@pytest.mark.adversarial
def test_a_padded_secret_is_stripped_before_use() -> None:
    credentials = load_broker_api_credentials(
        BrokerName.UPSTOX,
        {"UPSTOX_API_KEY": "  key  ", "UPSTOX_API_SECRET": "  secret  "},
    )
    assert credentials.api_key == "key"
    assert credentials.api_secret == "secret"


@pytest.mark.adversarial
def test_api_credentials_never_print_their_secrets() -> None:
    printed = repr(BrokerApiCredentials(BrokerName.ZERODHA_KITE, "live-key", "live-secret"))
    assert "live-key" not in printed
    assert "live-secret" not in printed
    assert "zerodha_kite" in printed, "the broker name is not a secret and aids debugging"


@pytest.mark.adversarial
def test_an_absent_secret_prints_as_none_not_as_masked() -> None:
    """`'***'` for an absent secret would make an unconfigured broker look configured."""
    printed = repr(BrokerApiCredentials(BrokerName.GROWW, "key", None))
    assert "api_secret=None" in printed


# ------------------------------------------------------------- Kite login credentials


_COMPLETE_LOGIN_ENVIRON = {
    "ZERODHA_KITE_USER_ID": "AB1234",
    "ZERODHA_KITE_PASSWORD": "pw",
    "ZERODHA_KITE_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
}


@pytest.mark.unit
def test_login_credentials_load_from_an_injected_environment() -> None:
    credentials = load_kite_login_credentials(_COMPLETE_LOGIN_ENVIRON)
    assert credentials.kite_user_id == "AB1234"
    assert credentials.kite_totp_secret == "JBSWY3DPEHPK3PXP"


@pytest.mark.adversarial
def test_all_missing_login_variables_are_reported_at_once() -> None:
    """Reporting them one at a time makes the operator run the login three times."""
    with pytest.raises(MissingBrokerCredentialsError) as failure:
        load_kite_login_credentials({})
    message = str(failure.value)
    for env_var in _COMPLETE_LOGIN_ENVIRON:
        assert env_var in message


@pytest.mark.adversarial
@pytest.mark.parametrize("blanked_var", sorted(_COMPLETE_LOGIN_ENVIRON))
def test_any_single_blank_login_variable_is_refused(blanked_var: str) -> None:
    environ = dict(_COMPLETE_LOGIN_ENVIRON) | {blanked_var: "   "}
    with pytest.raises(MissingBrokerCredentialsError, match=blanked_var):
        load_kite_login_credentials(environ)


@pytest.mark.adversarial
def test_a_padded_totp_secret_is_stripped() -> None:
    """A TOTP secret with a trailing newline produces valid-looking wrong codes."""
    environ = dict(_COMPLETE_LOGIN_ENVIRON) | {"ZERODHA_KITE_TOTP_SECRET": "JBSWY3DPEHPK3PXP\n"}
    assert load_kite_login_credentials(environ).kite_totp_secret == "JBSWY3DPEHPK3PXP"


@pytest.mark.adversarial
def test_login_credentials_never_print_password_or_totp_secret() -> None:
    printed = repr(KiteLoginCredentials("AB1234", "hunter2", "JBSWY3DPEHPK3PXP"))
    assert "hunter2" not in printed
    assert "JBSWY3DPEHPK3PXP" not in printed
    assert "AB1234" in printed, "the user id is not a secret and identifies the account"
