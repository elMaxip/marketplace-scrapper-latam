import smtplib
import ssl
import time
from collections import Counter
from dataclasses import dataclass
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from logging import Logger
from pathlib import Path
from typing import ClassVar, List, Tuple

import inflect
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from .ai import AIResponse  # type: ignore
from .listing import Listing
from .messages import HTML, PLAIN, ListingCard, build_card, summary_title
from .notification import NotificationConfig, NotificationStatus
from .utils import fetch_with_retry, hilight, resize_image_data


@dataclass
class EmailNotificationConfig(NotificationConfig):
    notify_method = "email"
    required_fields: ClassVar[List[str]] = ["email", "smtp_password"]

    email: List[str] | None = None
    smtp_server: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None

    def handle_email(self: "EmailNotificationConfig") -> None:
        if self.email is None:
            return
        if isinstance(self.email, str):
            self.email = [self.email]
        if not isinstance(self.email, list) or not all(
            (isinstance(x, str) and "@" in x and "." in x.split("@")[1]) for x in self.email
        ):
            raise ValueError(
                f"Item {hilight(self.name)} email must be a string or list of string."
            )

    def handle_smtp_server(self: "EmailNotificationConfig") -> None:
        if self.smtp_server is None:
            return

        if not isinstance(self.smtp_server, str):
            raise ValueError("user requires a string smtp_server.")
        self.smtp_server = self.smtp_server.strip()

    def handle_smtp_port(self: "EmailNotificationConfig") -> None:
        if self.smtp_port is None:
            return

        if not isinstance(self.smtp_port, int):
            raise ValueError("user requires an integer smtp_port.")
        if self.smtp_port < 1 or self.smtp_port > 65535:
            raise ValueError("user requires an integer smtp_port between 1 and 65535.")

    def handle_smtp_username(self: "EmailNotificationConfig") -> None:
        if self.smtp_username is None:
            return

        # smtp_username should be a string
        if not isinstance(self.smtp_username, str) or not self.smtp_username:
            raise ValueError("A non-empty value is requires for option smtp_username.")
        self.smtp_username = self.smtp_username.strip()

    def handle_smtp_password(self: "EmailNotificationConfig") -> None:
        if self.smtp_password is None:
            return

        # smtp_password should be a string
        if not isinstance(self.smtp_password, str) or not self.smtp_password:
            raise ValueError("A non-empty value is is required for option smtp_password.")
        self.smtp_password = self.smtp_password.strip()

    def handle_smtp_from(self: "EmailNotificationConfig") -> None:
        if self.smtp_from is None:
            return
        # smtp_from should be a string
        if not isinstance(self.smtp_from, str):
            raise ValueError("user requires a string smtp_from.")
        self.smtp_from = self.smtp_from.strip()

    def get_title(
        self: "EmailNotificationConfig",
        listings: List[Listing],
        notification_status: List[NotificationStatus],
        force: bool = False,
        cards: List[ListingCard] | None = None,
        language: str | None = None,
    ) -> str:
        """The subject line: how many, of what kind, from where.

        The one-kind case -- which is nearly every email -- is handed to
        :func:`~ai_marketplace_monitor.messages.summary_title` so the subject
        reads the same as the heading a push channel would send, in the same
        language.  A round that turned up several kinds at once keeps the older
        enumerating sentence, because there is no shorter honest way to say
        "two new, one cheaper and one worth another look".
        """
        counts = Counter(
            status for status in notification_status
            if force or status != NotificationStatus.NOTIFIED
        )
        if not counts:
            return ""
        if cards is not None and len(counts) == 1:
            only = next(iter(counts))
            return summary_title(
                [card for card, status in zip(cards, notification_status) if status == only],
                only.name,
                listings[0].marketplace,
                language=language,
            )
        p = inflect.engine()
        n_new = len([x for x in notification_status if x == NotificationStatus.NOT_NOTIFIED])
        n_notified = len([x for x in notification_status if x == NotificationStatus.NOTIFIED])
        n_expired = len([x for x in notification_status if x == NotificationStatus.EXPIRED])
        n_updated = len(
            [x for x in notification_status if x == NotificationStatus.LISTING_CHANGED]
        )
        n_discounted = len(
            [x for x in notification_status if x == NotificationStatus.LISTING_DISCOUNTED]
        )
        title = "Found "
        cnts = []
        if n_new > 0:
            cnts.append(f"{n_new} new ")
        if n_updated > 0:
            cnts.append(f"{n_updated} updated ")
        if n_discounted > 0:
            cnts.append(f"{n_discounted} discounted ")
        if n_expired > 0 or (force and n_notified > 0):
            cnts.append(f"{n_expired + (n_notified if force else 0)} revisitable ")
        if len(cnts) > 1:
            cnts[-1] = f"and {cnts[-1]}"
        elif len(cnts) == 0:
            # no new items
            return ""

        title += " ".join(cnts)
        title += f"{listings[0].name} {p.plural_noun('listing', len(listings) - (0 if force else n_notified))} from {listings[0].marketplace}"
        return title

    def get_text_message(
        self: "EmailNotificationConfig",
        listings: List[Listing],
        ratings: List[AIResponse],
        notification_status: List[NotificationStatus],
        force: bool = False,
        logger: Logger | None = None,
        cards: List[ListingCard] | None = None,
    ) -> str:
        """The plain-text half of the email: the same cards, unformatted.

        It is what a text-only client shows and what most clients quote when
        the message is replied to, so it says the same things in the same order
        as the HTML rather than a shorter, differently-organised version of
        them.
        """
        if cards is None:
            cards = [
                build_card(listing, rating, status)
                for listing, rating, status in zip(listings, ratings, notification_status)
            ]
        # One template for the batch, chosen from the first status that is
        # actually being sent: an email covers one round of one search, so the
        # batch is one kind of news the same way a push batch is.
        template = self._batch_template(notification_status, force)
        return "\n\n".join(
            card.render(PLAIN, template=template)
            for card, status in zip(cards, notification_status)
            if force or status != NotificationStatus.NOTIFIED
        )

    def _batch_template(
        self: "EmailNotificationConfig",
        notification_status: List[NotificationStatus],
        force: bool = False,
    ) -> str | None:
        """The user's wording for this email, or None for the built-in layout."""
        for status in notification_status:
            if force or status != NotificationStatus.NOTIFIED:
                return self.template_for(status)
        return None

    def get_html_message(
        self: "EmailNotificationConfig",
        listings: List[Listing],
        ratings: List[AIResponse],
        notification_status: List[NotificationStatus],
        force: bool = False,
        logger: Logger | None = None,
        cards: List[ListingCard] | None = None,
    ) -> Tuple[str, list[Tuple[bytes, str, str]]]:  # Return HTML and image data
        template_dir = Path(__file__).parent

        # Set up Jinja2 environment
        env = Environment(
            loader=FileSystemLoader(template_dir), autoescape=select_autoescape(["html", "xml"])
        )

        # Add custom filter for hashing
        env.filters["hash"] = hash

        def bold_headers(text: str) -> Markup:
            """Escape text then bold known section headers."""
            safe_text = escape(text)
            for header in ("About this vehicle", "Seller's description", "Description"):
                bold = Markup(f"<b>{header}</b>")  # noqa: S704 — header is a literal
                safe_text = safe_text.replace(escape(header), bold)
            return Markup(safe_text)  # noqa: S704 — safe_text was escaped above

        env.filters["bold_headers"] = bold_headers

        # Load template
        template = env.get_template("email.html.j2")

        # Prepare images list for attachments
        images = []
        valid_image_hashes = set()  # Track which images were successfully processed

        # Process images first
        for listing in listings:
            if listing.image:
                result = fetch_with_retry(listing.image, logger=logger)
                if result:
                    image_data, content_type = result
                    image_data = resize_image_data(image_data)
                    if image_data and len(image_data) <= 1024 * 1024:
                        image_hash = hash(listing.image)
                        images.append((image_data, content_type, f"image_{image_hash}"))
                        valid_image_hashes.add(image_hash)  # Track valid image
                    else:
                        if logger:
                            logger.debug(f"Image too large: {len(image_data)} bytes, skipped.")
                else:
                    if logger:
                        logger.debug(f"Failed to fetch image: {listing.image}")

        if cards is None:
            cards = [
                build_card(listing, rating, status)
                for listing, rating, status in zip(listings, ratings, notification_status)
            ]

        # A body per listing when the channel has a template, None otherwise.
        # Rendered here rather than in the Jinja template because the escaping
        # is the card's business, not the layout's -- and HTML is the one format
        # where getting that wrong is a security question and not a cosmetic one.
        wording = self._batch_template(notification_status, force)
        bodies = [
            Markup(card.render(HTML, template=wording)) if wording else None
            for card in cards
        ]

        # Render template.  The card travels alongside the listing rather than
        # replacing it: the template still needs the raw listing for the image
        # it has already fetched and attached by content id.
        html = template.render(
            listings=zip(listings, ratings, notification_status, cards, bodies),
            force=force,
            item_name=listings[0].name.capitalize(),
            NotificationStatus=NotificationStatus,  # Pass enum for comparison
            valid_image_hashes=valid_image_hashes,  # Pass set of valid image hashes
        )
        return html, images

    def notify(
        self: "EmailNotificationConfig",
        listings: List[Listing],
        ratings: List[AIResponse],
        notification_status: List[NotificationStatus],
        force: bool = False,
        logger: Logger | None = None,
        cards: List[ListingCard] | None = None,
        language: str | None = None,
        description_words: int | None = None,
    ) -> bool:
        """Send one email covering every listing this round turned up.

        Unlike the push channels, an email is a place where a picture and a
        paragraph both fit, so this one keeps its HTML template.  What it takes
        from the cards is the part it could not work out for itself: what the
        listing cost last time, and how far it has moved since.

        ``description_words`` is accepted and deliberately not applied to the
        HTML body.  The limit exists because a phone notification is read in a
        second and because Telegram refuses a long message outright; an email
        has neither problem, and it is the copy that gets kept and searched, so
        it carries the seller's whole text.  The cards it renders in the plain
        text half were already shortened by whoever built them.
        """
        if not self._has_required_fields():
            if logger:
                logger.debug(
                    f"Missing required fields {', '.join(self.required_fields)}. No {self.notify_method} notification sent."
                )
            return False

        if cards is None:
            cards = [
                build_card(listing, rating, status, language=language)
                for listing, rating, status in zip(listings, ratings, notification_status)
            ]

        title = self.get_title(listings, notification_status, force=force, cards=cards,
                               language=language)
        if not title:
            if logger:
                logger.debug("No new listings. No email sent.")
            return False
        message = self.get_text_message(
            listings, ratings, notification_status, force, logger=logger, cards=cards
        )
        html_message, images = self.get_html_message(
            listings, ratings, notification_status, force, logger=logger, cards=cards
        )
        return self.send_email_message(title, message, html_message, images, logger=logger)

    def send_email_message(
        self: "EmailNotificationConfig",
        title: str,
        message: str,
        html: str,
        images: List[Tuple[bytes, str, str]],
        logger: Logger | None = None,
    ) -> bool:
        if not self.email:
            if logger:
                logger.debug("No recipients specified. No email sent.")
            return False

        sender = self.smtp_from or self.smtp_username or self.email[0]

        if self.smtp_server:
            smtp_server = self.smtp_server
        else:
            smtp_server = f"""smtp.{sender.split("@")[1]}"""

        # s.starttls()
        msg = MIMEMultipart("related")
        msg["Subject"] = title
        # can use the humanized version of self.name as well
        msg["From"] = formataddr(("AI Marketplace Monitor", sender))
        msg["To"] = ", ".join(self.email)

        # Create alternative part
        alt_part = MIMEMultipart("alternative")
        msg.attach(alt_part)

        alt_part.attach(MIMEText(message, "plain"))
        alt_part.attach(MIMEText(html, "html"))  # HTML part last = preferred

        # Attach images
        for image_data, _, cid in images:
            image = MIMEImage(image_data)
            image.add_header("Content-ID", f"<{cid}>")
            image.add_header("Content-Disposition", "inline")
            msg.attach(image)

        for attempt in range(self.max_retries):
            try:
                smtp_port = self.smtp_port or 587
                smtp_username = self.smtp_username or sender
                if not smtp_username:
                    if logger:
                        logger.error("No smtp username.")
                    return False

                smtp_password = self.smtp_password
                if not smtp_password:
                    if logger:
                        logger.error("No smtp password.")
                    return False

                context = ssl.create_default_context()
                with smtplib.SMTP(smtp_server, smtp_port) as smtp:
                    # smtp.set_debuglevel(1)
                    smtp.ehlo()  # Can be omitted
                    smtp.starttls(context=context)
                    smtp.ehlo()  # Can be omitted
                    try:
                        smtp.login(smtp_username, smtp_password)
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        if logger:
                            logger.error(
                                f"Failed to login to smtp server {smtp_server}:{smtp_port} with username {smtp_username}: {e}"
                            )
                        return False
                    smtp.send_message(msg)
                if logger:
                    logger.info(
                        f"""{hilight("[Notify]", "succ")} Sent {self.name} an email with title {hilight(title)}"""
                    )
                return True
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if logger:
                    logger.debug(
                        f"""{hilight("[Notify]", "fail")} Attempt {attempt + 1} failed: {e}"""
                    )
                if attempt < self.max_retries - 1:
                    if logger:
                        logger.debug(
                            f"""{hilight("[Notify]", "fail")} Retrying in {self.retry_delay} seconds..."""
                        )
                    time.sleep(self.retry_delay)
                else:
                    if logger:
                        logger.error(
                            f"""{hilight("[Notify]", "fail")} Max retries reached. Failed to push note to {self.name}."""
                        )
                    return False
        return False
