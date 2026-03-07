"""Gotify notification platform for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.components.notify import (
    ATTR_DATA,
    ATTR_TARGET,
    ATTR_TITLE,
    BaseNotificationService,
)
from homeassistant.const import CONF_NAME, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_TITLE = "title"
CONF_TOKEN = "token"
CONF_PRIORITY = "priority"
CONF_DEFAULT_PRIORITY = "default_priority"
CONF_EXTRAS = "extras"
CONF_FORWARD_PERSISTENT_NOTIFICATIONS = "forward_persistent_notifications"

DEFAULT_NAME = "gotify"
DEFAULT_TITLE = "Home Assistant"
DEFAULT_PRIORITY = 5

DATA_SERVICES = "services"
DATA_UNSUB = "persistent_notification_unsub"
DATA_LAST_SENT = "last_sent_notifications"
DATA_PENDING_TASKS = "pending_tasks"
DATA_PENDING_PAYLOADS = "pending_payloads"
PERSISTENT_NOTIFICATION_DEBOUNCE_SECONDS = 120
PERSISTENT_NOTIFICATION_TYPES = {
    persistent_notification.UpdateType.ADDED,
    persistent_notification.UpdateType.UPDATED,
    persistent_notification.UpdateType.REMOVED,
}

PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_URL): cv.url,
        vol.Required(CONF_TOKEN): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_TITLE, default=DEFAULT_TITLE): cv.string,
        vol.Optional(CONF_DEFAULT_PRIORITY, default=DEFAULT_PRIORITY): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
        vol.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
        vol.Optional(CONF_FORWARD_PERSISTENT_NOTIFICATIONS, default=True): cv.boolean,
    }
)


async def async_get_service(hass, config, discovery_info=None):
    """Set up the Gotify notification service."""
    service = HAGotifyNotificationService(
        session=async_get_clientsession(hass, verify_ssl=config[CONF_VERIFY_SSL]),
        base_url=config[CONF_URL],
        token=config[CONF_TOKEN],
        title=config[CONF_TITLE],
        default_priority=config[CONF_DEFAULT_PRIORITY],
    )
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data.setdefault(DATA_SERVICES, []).append(service)

    if config[CONF_FORWARD_PERSISTENT_NOTIFICATIONS]:
        _async_register_persistent_notification_forwarder(hass)

    return service


def _async_register_persistent_notification_forwarder(hass) -> None:
    """Register persistent notification forwarding once."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if DATA_UNSUB in domain_data:
        return

    @callback
    def _handle_persistent_notification_update(update_type, notifications) -> None:
        if update_type not in PERSISTENT_NOTIFICATION_TYPES:
            return

        hass.async_create_task(
            _async_forward_persistent_notifications(hass, update_type, notifications)
        )

    unsub = persistent_notification.async_register_callback(
        hass, _handle_persistent_notification_update
    )
    domain_data[DATA_UNSUB] = unsub


async def _async_forward_persistent_notifications(
    hass, update_type, notifications
) -> None:
    """Forward Home Assistant persistent notifications to Gotify."""
    domain_data = hass.data.get(DOMAIN, {})
    services = list(domain_data.get(DATA_SERVICES, []))
    if not services:
        return

    last_sent = domain_data.setdefault(DATA_LAST_SENT, {})
    pending_tasks = domain_data.setdefault(DATA_PENDING_TASKS, {})
    pending_payloads = domain_data.setdefault(DATA_PENDING_PAYLOADS, {})

    if update_type == persistent_notification.UpdateType.REMOVED:
        for notification in notifications.values():
            notification_key = _notification_key(notification)
            if notification_key is None:
                continue

            task = pending_tasks.pop(notification_key, None)
            if task is not None:
                task.cancel()
            pending_payloads.pop(notification_key, None)
            last_sent.pop(notification_key, None)
        return

    for notification in notifications.values():
        title = _notification_field(
            notification, persistent_notification.ATTR_TITLE, DEFAULT_TITLE
        )
        message = _notification_field(
            notification, persistent_notification.ATTR_MESSAGE, ""
        )
        notification_id = _notification_field(
            notification, persistent_notification.ATTR_NOTIFICATION_ID, None
        )
        notification_key = _notification_key(notification)
        if notification_key is None:
            continue

        signature = (title, message)
        extras = {
            "homeassistant::persistent_notification": {
                "update_type": str(update_type),
                "notification_id": notification_id,
            }
        }
        pending_payloads[notification_key] = {
            "message": message,
            "title": title,
            "data": {"extras": extras},
            "signature": signature,
        }

        if notification_key not in pending_tasks or pending_tasks[notification_key].done():
            pending_tasks[notification_key] = hass.async_create_task(
                _async_debounced_send(hass, notification_key)
            )


async def _async_debounced_send(hass, notification_key: str) -> None:
    """Send only the latest version of a persistent notification after a delay."""
    try:
        await asyncio.sleep(PERSISTENT_NOTIFICATION_DEBOUNCE_SECONDS)

        domain_data = hass.data.get(DOMAIN, {})
        services = list(domain_data.get(DATA_SERVICES, []))
        pending_payloads = domain_data.get(DATA_PENDING_PAYLOADS, {})
        last_sent = domain_data.get(DATA_LAST_SENT, {})
        payload = pending_payloads.get(notification_key)

        if not services or payload is None:
            return

        if last_sent.get(notification_key) == payload["signature"]:
            return

        for service in services:
            await service.async_send_message(
                message=payload["message"],
                title=payload["title"],
                data=payload["data"],
            )

        last_sent[notification_key] = payload["signature"]
    except asyncio.CancelledError:
        raise
    finally:
        domain_data = hass.data.get(DOMAIN, {})
        pending_tasks = domain_data.get(DATA_PENDING_TASKS, {})
        pending_payloads = domain_data.get(DATA_PENDING_PAYLOADS, {})
        pending_tasks.pop(notification_key, None)
        pending_payloads.pop(notification_key, None)


def _notification_field(notification: Any, field: str, default: Any) -> Any:
    """Read a field from a persistent notification object or dict."""
    if isinstance(notification, dict):
        return notification.get(field, default)
    return getattr(notification, field, default)


def _notification_key(notification: Any) -> str | None:
    """Build a stable key for a persistent notification."""
    notification_id = _notification_field(
        notification, persistent_notification.ATTR_NOTIFICATION_ID, None
    )
    if notification_id is not None:
        return str(notification_id)

    title = _notification_field(notification, persistent_notification.ATTR_TITLE, None)
    if title is not None:
        return f"title:{title}"

    return None


class HAGotifyNotificationService(BaseNotificationService):
    """Send notifications to a Gotify server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
        title: str,
        default_priority: int,
    ) -> None:
        """Initialize the service."""
        self._session = session
        self._message_url = f"{base_url.rstrip('/')}/message?token={token}"
        self._title = title
        self._default_priority = default_priority

    async def async_send_message(self, message: str = "", **kwargs: Any) -> None:
        """Send a message to Gotify."""
        data = dict(kwargs.get(ATTR_DATA) or {})
        target = kwargs.get(ATTR_TARGET) or []
        title = kwargs.get(ATTR_TITLE) or self._title
        priority = data.pop(CONF_PRIORITY, self._default_priority)
        extras = data.pop(CONF_EXTRAS, None)

        if isinstance(target, str):
            target = [target]

        if target:
            title = f"{title} ({', '.join(target)})"

        payload: dict[str, Any] = {
            "message": message or "",
            "title": title,
            "priority": priority,
        }
        if isinstance(extras, dict):
            payload["extras"] = extras
        if data:
            payload["extras"] = {
                **payload.get("extras", {}),
                "homeassistant::data": data,
            }

        try:
            async with self._session.post(self._message_url, json=payload) as response:
                if response.status >= 400:
                    body = await response.text()
                    _LOGGER.error(
                        "Gotify notification failed with status %s: %s",
                        response.status,
                        body,
                    )
        except aiohttp.ClientError as err:
            _LOGGER.error("Error sending notification to Gotify: %s", err)
