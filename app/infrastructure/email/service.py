import logging
from abc import ABC, abstractmethod

logger = logging.getLogger("app.email")


class EmailService(ABC):
    @abstractmethod
    def send(self, to_email: str, subject: str, body: str) -> None:
        raise NotImplementedError


class ConsoleEmailService(EmailService):
    """Phase 1 adapter — logs emails instead of sending SMTP."""

    def send(self, to_email: str, subject: str, body: str) -> None:
        logger.info("EMAIL to=%s subject=%s\n%s", to_email, subject, body)
        print(f"\n=== EMAIL ===\nTo: {to_email}\nSubject: {subject}\n{body}\n=============\n")


email_service: EmailService = ConsoleEmailService()
