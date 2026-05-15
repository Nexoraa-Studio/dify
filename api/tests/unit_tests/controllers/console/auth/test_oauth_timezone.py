from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from controllers.console.auth.oauth import OAUTH_INIT_STATE_COOKIE_NAME, OAuthCallback, OAuthLogin, _generate_account
from libs.oauth import OAuthUserInfo, encode_oauth_state
from models.account import AccountStatus
from services.errors.account import AccountRegisterError


@pytest.fixture
def app() -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    return app


@patch("controllers.console.auth.oauth.redirect")
@patch("controllers.console.auth.oauth.get_oauth_providers")
def test_oauth_login_passes_language_and_timezone_to_authorization_url(
    mock_get_oauth_providers,
    mock_redirect,
    app: Flask,
):
    oauth_provider = MagicMock()
    oauth_provider.get_authorization_url.return_value = "https://github.com/login/oauth/authorize?state=..."
    mock_get_oauth_providers.return_value = {"github": oauth_provider}

    with app.test_request_context("/oauth/login/github?language=zh-Hans&timezone=Asia/Shanghai"):
        OAuthLogin().get("github")

    oauth_provider.get_authorization_url.assert_called_once_with(
        invite_token=None,
        timezone="Asia/Shanghai",
        language="zh-Hans",
    )
    mock_redirect.assert_called_once_with("https://github.com/login/oauth/authorize?state=...")


@patch("controllers.console.auth.oauth.get_oauth_providers")
def test_oauth_login_sets_init_state_cookie(
    mock_get_oauth_providers,
    app: Flask,
):
    oauth_provider = MagicMock()
    oauth_provider.get_authorization_url.return_value = "https://github.com/login/oauth/authorize?state=..."
    mock_get_oauth_providers.return_value = {"github": oauth_provider}

    with app.test_request_context("/oauth/login/github?language=zh-Hans&timezone=Asia/Shanghai"):
        response = OAuthLogin().get("github")

    set_cookie_headers = response.headers.getlist("Set-Cookie")
    assert any(header.startswith(f"{OAUTH_INIT_STATE_COOKIE_NAME}=") for header in set_cookie_headers)
    assert any("HttpOnly" in header and "SameSite=Lax" in header for header in set_cookie_headers)


@pytest.mark.parametrize(
    ("state", "cookie_state", "expected_timezone", "expected_language"),
    [
        (None, encode_oauth_state(timezone="Asia/Shanghai", language="zh-Hans"), "Asia/Shanghai", "zh-Hans"),
        (
            encode_oauth_state(timezone="Europe/Paris", language="fr-FR"),
            encode_oauth_state(timezone="Asia/Shanghai", language="zh-Hans"),
            "Europe/Paris",
            "fr-FR",
        ),
    ],
)
@patch("controllers.console.auth.oauth.set_csrf_token_to_cookie")
@patch("controllers.console.auth.oauth.set_refresh_token_to_cookie")
@patch("controllers.console.auth.oauth.set_access_token_to_cookie")
@patch("controllers.console.auth.oauth.AccountService")
@patch("controllers.console.auth.oauth.TenantService")
@patch("controllers.console.auth.oauth._generate_account")
@patch("controllers.console.auth.oauth.get_oauth_providers")
def test_oauth_callback_uses_state_then_cookie_for_registration_preferences(
    mock_get_oauth_providers,
    mock_generate_account,
    mock_tenant_service,
    mock_account_service,
    mock_set_access_token,
    mock_set_refresh_token,
    mock_set_csrf_token,
    state,
    cookie_state,
    expected_timezone,
    expected_language,
    app: Flask,
):
    oauth_provider = MagicMock()
    oauth_provider.get_access_token.return_value = "access-token"
    oauth_provider.get_user_info.return_value = OAuthUserInfo(
        id="github-123",
        name="Test User",
        email="user@example.com",
    )
    mock_get_oauth_providers.return_value = {"github": oauth_provider}

    account = MagicMock()
    account.status = AccountStatus.ACTIVE
    mock_generate_account.return_value = (account, True)
    mock_account_service.login.return_value = SimpleNamespace(
        access_token="access-token",
        refresh_token="refresh-token",
        csrf_token="csrf-token",
    )

    query = "code=test-code"
    if state:
        query = f"{query}&state={state}"

    headers = {"Cookie": f"{OAUTH_INIT_STATE_COOKIE_NAME}={cookie_state}"}
    with (
        patch("controllers.console.auth.oauth.dify_config.CONSOLE_WEB_URL", "http://localhost:3000"),
        app.test_request_context(f"/oauth/authorize/github?{query}", headers=headers),
    ):
        response = OAuthCallback().get("github")

    mock_generate_account.assert_called_once_with(
        "github",
        oauth_provider.get_user_info.return_value,
        timezone=expected_timezone,
        language=expected_language,
    )
    mock_tenant_service.create_owner_tenant_if_not_exist.assert_called_once_with(account)
    mock_account_service.login.assert_called_once()
    assert response.status_code == 302
    assert response.headers["Location"] == "http://localhost:3000?oauth_new_user=true"
    assert any(
        header.startswith(f"{OAUTH_INIT_STATE_COOKIE_NAME}=;") and "Expires=Thu, 01 Jan 1970 00:00:00 GMT" in header
        for header in response.headers.getlist("Set-Cookie")
    )


@patch("controllers.console.auth.oauth.AccountService.link_account_integrate")
@patch("controllers.console.auth.oauth.RegisterService")
@patch("controllers.console.auth.oauth.FeatureService")
@patch("controllers.console.auth.oauth._get_account_by_openid_or_email", return_value=None)
def test_generate_account_registers_with_browser_timezone(
    mock_get_account,
    mock_feature_service,
    mock_register_service,
    mock_link_account,
    app: Flask,
):
    account = MagicMock()
    mock_register_service.register.return_value = account
    mock_feature_service.get_system_features.return_value.is_allow_register = True
    user_info = OAuthUserInfo(id="github-123", name="Test User", email="User@Example.com")

    with app.test_request_context(headers={"Accept-Language": "zh-Hans,zh;q=0.9"}):
        result, oauth_new_user = _generate_account("github", user_info, timezone="Asia/Shanghai")

    assert result is account
    assert oauth_new_user is True
    mock_register_service.register.assert_called_once_with(
        email="user@example.com",
        name="Test User",
        password=None,
        open_id="github-123",
        provider="github",
        language="zh-Hans",
        timezone="Asia/Shanghai",
    )
    mock_link_account.assert_called_once_with("github", "github-123", account)


@patch("controllers.console.auth.oauth.AccountService.link_account_integrate")
@patch("controllers.console.auth.oauth.RegisterService")
@patch("controllers.console.auth.oauth.FeatureService")
@patch("controllers.console.auth.oauth._get_account_by_openid_or_email", return_value=None)
def test_generate_account_prefers_state_language_over_accept_language(
    mock_get_account,
    mock_feature_service,
    mock_register_service,
    mock_link_account,
    app: Flask,
):
    account = MagicMock()
    mock_register_service.register.return_value = account
    mock_feature_service.get_system_features.return_value.is_allow_register = True
    user_info = OAuthUserInfo(id="github-123", name="Test User", email="User@Example.com")

    with app.test_request_context(headers={"Accept-Language": "en-US,en;q=0.9"}):
        _generate_account("github", user_info, language="zh-Hans")

    mock_register_service.register.assert_called_once_with(
        email="user@example.com",
        name="Test User",
        password=None,
        open_id="github-123",
        provider="github",
        language="zh-Hans",
        timezone=None,
    )
    mock_link_account.assert_called_once_with("github", "github-123", account)


@patch("controllers.console.auth.oauth.dify_config")
@patch("controllers.console.auth.oauth.RegisterService")
@patch("controllers.console.auth.oauth.FeatureService")
@patch("controllers.console.auth.oauth._get_account_by_openid_or_email", return_value=None)
def test_generate_account_rejects_new_user_when_registration_disabled(
    mock_get_account,
    mock_feature_service,
    mock_register_service,
    mock_config,
    app: Flask,
):
    mock_feature_service.get_system_features.return_value.is_allow_register = False
    mock_config.BILLING_ENABLED = False
    user_info = OAuthUserInfo(id="github-123", name="Test User", email="user@example.com")

    with app.test_request_context(headers={"Accept-Language": "en-US,en;q=0.9"}):
        with pytest.raises(AccountRegisterError):
            _generate_account("github", user_info)

    mock_register_service.register.assert_not_called()
