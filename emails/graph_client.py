import json
from dataclasses import dataclass

import msal
import requests
from django.conf import settings


class GraphEmailError(RuntimeError):
    pass


@dataclass(frozen=True)
class GraphConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    sender: str


def _get_graph_config() -> GraphConfig:
    tenant_id = (getattr(settings, "MS_GRAPH_TENANT_ID", "") or "").strip()
    client_id = (getattr(settings, "MS_GRAPH_CLIENT_ID", "") or "").strip()
    client_secret = (getattr(settings, "MS_GRAPH_CLIENT_SECRET", "") or "").strip()
    sender = (getattr(settings, "MS_GRAPH_SENDER", "") or "").strip()
    if not (tenant_id and client_id and client_secret and sender):
        raise GraphEmailError("Microsoft Graph email is not configured (missing MS_GRAPH_* settings).")
    return GraphConfig(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        sender=sender,
    )


def acquire_app_token() -> str:
    cfg = _get_graph_config()
    authority = f"https://login.microsoftonline.com/{cfg.tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id=cfg.client_id,
        authority=authority,
        client_credential=cfg.client_secret,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    access_token = result.get("access_token")
    if not access_token:
        raise GraphEmailError(f"Unable to acquire token: {json.dumps(result, ensure_ascii=False)}")
    return access_token


def send_mail_via_graph(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    html: str,
    text: str | None = None,
    reply_to: str | None = None,
) -> None:
    """
    Envoie un email via Graph en mode app-only.
    L'expéditeur réel est settings.MS_GRAPH_SENDER (mailbox dédiée).
    """
    cfg = _get_graph_config()
    token = acquire_app_token()

    url = f"https://graph.microsoft.com/v1.0/users/{cfg.sender}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    body_content = html or (text or "")
    content_type = "HTML" if html else "Text"

    message: dict = {
        "subject": subject,
        "body": {"contentType": content_type, "content": body_content},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
    }

    # Permet de garder une logique `from_email` côté produit (noreply@..., support@...)
    # Même si Graph enverra réellement depuis la mailbox `MS_GRAPH_SENDER`.
    if reply_to:
        message["replyTo"] = [{"emailAddress": {"address": reply_to}}]
    elif from_email and from_email != cfg.sender:
        message["replyTo"] = [{"emailAddress": {"address": from_email}}]

    payload = {"message": message, "saveToSentItems": True}

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    if resp.status_code not in (202, 200):
        raise GraphEmailError(f"Graph sendMail failed ({resp.status_code}): {resp.text}")

