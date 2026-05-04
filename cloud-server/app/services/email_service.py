from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage

from app.core.config import get_settings


class EmailDeliveryError(RuntimeError):
    pass


def send_verification_code_email(*, to_email: str, code: str) -> None:
    settings = get_settings()
    provider = settings.email_provider.strip().lower()
    if provider == "tencent_ses":
        _send_with_tencent_ses(to_email=to_email, code=code)
        return
    if provider != "smtp":
        raise EmailDeliveryError(f"Unsupported email provider: {settings.email_provider}")
    _send_with_smtp(to_email=to_email, code=code)


def _verification_template_data(*, code: str) -> str:
    return json.dumps(
        {
            "code": code,
        },
        ensure_ascii=False,
    )


def _send_with_smtp(*, to_email: str, code: str) -> None:
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_from:
        raise EmailDeliveryError("SMTP is not configured")

    message = EmailMessage()
    message["Subject"] = "Surfaced 管理账号邮箱验证码"
    message["From"] = settings.smtp_from
    message["To"] = to_email
    message.set_content(
        "\n".join(
            [
                "你的 Surfaced 管理账号验证码是：",
                "",
                code,
                "",
                f"验证码 {settings.email_code_ttl_minutes} 分钟内有效。若非本人操作，请忽略这封邮件。",
            ]
        )
    )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as client:
            if settings.smtp_use_tls:
                client.starttls()
            if settings.smtp_username or settings.smtp_password:
                client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(message)
    except Exception as exc:
        raise EmailDeliveryError(f"Verification email failed: {exc}") from exc


def _send_with_tencent_ses(*, to_email: str, code: str) -> None:
    settings = get_settings()
    missing = [
        name
        for name, value in {
            "SURFACED_CLOUD_TENCENT_SES_SECRET_ID": settings.tencent_ses_secret_id,
            "SURFACED_CLOUD_TENCENT_SES_SECRET_KEY": settings.tencent_ses_secret_key,
            "SURFACED_CLOUD_TENCENT_SES_FROM": settings.tencent_ses_from,
            "SURFACED_CLOUD_TENCENT_SES_TEMPLATE_ID": settings.tencent_ses_template_id,
        }.items()
        if not value
    ]
    if missing:
        raise EmailDeliveryError(f"Tencent SES is not configured: {', '.join(missing)}")

    try:
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.ses.v20201002 import models, ses_client
    except Exception as exc:
        raise EmailDeliveryError("Tencent SES SDK is not installed") from exc

    try:
        cred = credential.Credential(settings.tencent_ses_secret_id, settings.tencent_ses_secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = settings.tencent_ses_endpoint
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        client = ses_client.SesClient(cred, settings.tencent_ses_region, client_profile)

        request = models.SendEmailRequest()
        request.FromEmailAddress = settings.tencent_ses_from
        request.Destination = [to_email]
        request.Subject = "Surfaced 管理账号邮箱验证码"
        request.Template = models.Template()
        request.Template.TemplateID = settings.tencent_ses_template_id
        request.Template.TemplateData = _verification_template_data(code=code)
        request.TriggerType = 1
        if settings.tencent_ses_reply_to:
            request.ReplyToAddresses = settings.tencent_ses_reply_to

        client.SendEmail(request)
    except TencentCloudSDKException as exc:
        raise EmailDeliveryError(f"Tencent SES verification email failed: {exc}") from exc
    except Exception as exc:
        raise EmailDeliveryError(f"Tencent SES verification email failed: {exc}") from exc
