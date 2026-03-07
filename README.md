# HA Gotify

Forward Home Assistant notifications to [Gotify](https://gotify.net/).

This custom notification platform adds a `notify.gotify` service and can also forward Home Assistant `persistent_notification` messages automatically.

## Features

- Send Home Assistant notifications to Gotify
- Forward system notifications from `persistent_notification`
- Debounce repeated updates and send only the latest version after 2 minutes

## Installation

Copy this repository into your Home Assistant custom components directory:

```text
config/custom_components/ha_gotify/
```

Then restart Home Assistant.

## Configuration

Add this to `configuration.yaml`:

```yaml
notify:
  - platform: ha_gotify
    name: gotify
    url: "https://gotify.example.com"
    token: !secret gotify_token
    title: "Home Assistant"
    default_priority: 5
    verify_ssl: true
    forward_persistent_notifications: true
```

## Options

- `url`: Gotify server URL, without `/message`
- `token`: Gotify application token
- `name`: Home Assistant notify service name, default `gotify`
- `title`: Default notification title
- `default_priority`: Default Gotify priority, default `5`
- `verify_ssl`: Verify SSL certificate, default `true`
- `forward_persistent_notifications`: Forward Home Assistant system notifications, default `true`

## Usage

After setup, Home Assistant exposes:

```text
notify.gotify
```

Example service call:

```yaml
action:
  - service: notify.gotify
    data:
      title: "Doorbell"
      message: "Someone is at the door"
      data:
        priority: 8
```

## Persistent Notification Forwarding

When `forward_persistent_notifications` is enabled, the integration listens for Home Assistant `persistent_notification` updates.

- New or updated notifications are delayed by 120 seconds
- If the same notification changes again during that window, only the latest version is sent
- If the notification is removed before the delay ends, nothing is sent

This is useful for noisy notifications such as repeated device offline warnings.

## API

Messages are sent to Gotify with:

```text
POST /message?token=<app-token>
```
