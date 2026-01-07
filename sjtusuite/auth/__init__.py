"""Authentication layer for SJTU services."""
from .jac_login import JACLogin, extract_auth_params, get_test_jac_login
from .oauth_base import OAuthClientBase
