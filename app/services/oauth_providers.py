"""
OAuth provider configs for the three OAuth-based connectors (Google,
Microsoft Graph, Salesforce) — the endpoints/scopes app/services/oauth.py's
generic flow needs per provider. Client id/secret are always user-supplied
(an OAuth app registration is an action only the user's own Azure/Google
Cloud/Salesforce admin console can do — nothing here can create one).

Salesforce is the one provider whose token endpoint isn't fixed: it's
per-org (either the user's custom domain, or login.salesforce.com for
production / test.salesforce.com for sandboxes) — the connector's config
must include `instance_url` (or defaults to login.salesforce.com) before a
flow can start.
"""
from __future__ import annotations

OAUTH_CONNECTOR_TYPES = ("google", "microsoft_graph", "salesforce")

# Per-feature Graph delegated permissions a microsoft_graph connector can
# request, selected via config["scopes"] (a list of these keys) rather than
# one fixed string that grows narrower-by-default-violating every time a new
# Graph feature is added to this app. Keeping each connector's OAuth grant
# scoped to only what it's configured to use matches the existing "narrower
# than a full Workspace grant on purpose" precedent for Google below.
GRAPH_SCOPE_FEATURES = {
    "teams": "ChannelMessage.Send",
    "planner": "Tasks.ReadWrite",
    "calendar": "Calendars.ReadWrite",
    "files": "Files.ReadWrite",
}
# Connectors created before config["scopes"] existed had no way to select
# features — this is exactly the original fixed scope string, so an
# already-configured connector's behavior doesn't change until the user
# explicitly picks features in the UI.
_DEFAULT_GRAPH_FEATURES = ("teams", "planner")


def _graph_scope_string(config: dict) -> str:
    config = config or {}
    # `"scopes" in config` (not truthiness) distinguishes "never configured"
    # from "explicitly configured to zero features" — an `or` on the value
    # would treat a deliberately-empty [] the same as unset and silently
    # re-grant the teams+planner default the user just chose not to have.
    features = config["scopes"] if "scopes" in config else list(_DEFAULT_GRAPH_FEATURES)
    perms = [GRAPH_SCOPE_FEATURES[f] for f in (features or []) if f in GRAPH_SCOPE_FEATURES]
    # offline_access is required for a refresh_token at all on Graph, and is
    # always requested regardless of which features are selected.
    return "offline_access " + " ".join(perms)


def get_provider_config(connector_type: str, config: dict) -> dict:
    """Return {authorize_endpoint, token_endpoint, scope, extra_authorize_params}
    for `connector_type`. `config` is the connector's own (unencrypted) config
    dict — used for the one provider (Salesforce) whose endpoint isn't fixed,
    and for microsoft_graph's per-connector feature-scoped grant."""
    if connector_type == "google":
        return {
            "authorize_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
            # Tasks scope: create_google_task's action. Narrower than a full
            # Google Workspace scope grant on purpose.
            "scope": "https://www.googleapis.com/auth/tasks",
            # access_type=offline + prompt=consent are both required for Google
            # to reliably return a refresh_token — omitting either is the
            # single most common cause of the "no refresh_token" failure this
            # app hard-fails on (see oauth.py::exchange_code_for_tokens).
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        }
    if connector_type == "microsoft_graph":
        tenant = (config or {}).get("tenant_id") or "common"
        return {
            "authorize_endpoint": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
            "token_endpoint": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            "scope": _graph_scope_string(config),
            "extra_authorize_params": {},
        }
    if connector_type == "salesforce":
        instance = (config or {}).get("instance_url") or "https://login.salesforce.com"
        instance = instance.rstrip("/")
        return {
            "authorize_endpoint": f"{instance}/services/oauth2/authorize",
            "token_endpoint": f"{instance}/services/oauth2/token",
            "scope": "api refresh_token",
            "extra_authorize_params": {},
        }
    raise ValueError(f"Not an OAuth connector type: {connector_type}")
