"""Confirmed Telegram delivery boundary shared by production workflows."""

from utils.logger import get_logger


logger = get_logger(__name__)


def send_telegram_text(message: str) -> bool:
    """Send text and report success only when the transport confirms delivery.

    Exceptions can contain credential-bearing URLs, so their text is not logged.
    Callers remain responsible for recording their workflow's completion status.

    Args:
        message: The complete generated message to send.

    Returns:
        True only for an explicitly confirmed transport result; otherwise False.
    """
    try:
        from scripts.telegram_bot import send_text

        delivered = send_text(message) is True
    except Exception:
        logger.warning("Telegram delivery failed; transport did not confirm delivery")
        return False

    if delivered:
        logger.info("Telegram delivery confirmed")
    else:
        logger.warning("Telegram delivery was not confirmed or is not configured")
    return delivered
