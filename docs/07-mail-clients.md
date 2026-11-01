# Mail Clients: Thunderbird and FairEmail

Date: 2026-10-31  
Short settings overview for SMTP sending. This stack has **no IMAP/POP3** and no mailboxes for end users.

## SMTP Values

| Field | Value |
|---|---|
| Server / hostname | `mail.example.com` |
| Port | `465` (recommended) or `587` |
| Connection security | `SSL/TLS` on 465, `STARTTLS` on 587 |
| Authentication | `Normal password` |
| Username | `smtp@mail.example.com` |
| Password | the SMTP password set during bootstrap |
| From / sender | any address within the allowed domains |

Allowed sender domains:

```text
example.org
example.net
example.com
example.info
```

The local part is arbitrary, e.g. `user@example.org`, `info@example.net` or `typo@example.info`.

`mail.example.com` is a CNAME to `mx.example.com` and is included in the TLS certificate. `mx.example.com` remains the canonical HELO/PTR hostname of the server; mail clients can use `mail.example.com`.

## Do Not Use

- Port 25 (not published, not a submission port)
- "No encryption"
- OAuth2, Kerberos or other auth methods
- `smtp@mail.example.com` as the visible From address (that is only the login)

## Thunderbird

1. **Account Settings -> Outgoing Server (SMTP) -> Add**
2. Enter:

```text
Description:          k8s-mail-outbound
Server Name:          mail.example.com
Port:                 465
User Name:            smtp@mail.example.com
Authentication:       Normal password
Connection Security:  SSL/TLS
```

3. Save and assign it to the account or identity.
4. Under **Manage Identities** enter the desired From address, e.g. `user@example.org`.
5. Send a test mail.

For port 587:

```text
Port:                 587
Connection Security:  STARTTLS
Authentication:       Normal password
```

Thunderbird normally needs an incoming server for an account. Since this stack offers no IMAP/POP3, either use an existing mailbox as the account and only override the SMTP server, or add the senders as identities in an existing account. Sending is independent of that.

## FairEmail

1. **Manual setup / SMTP**
2. Enter:

```text
Host:                mail.example.com
Port:                465
Encryption:          SSL/TLS
Authentication:      Password / Normal password
Username:            smtp@mail.example.com
Password:            <SMTP_PASSWORD>
```

3. Use an allowed domain as the sender address.
4. For 587, choose `STARTTLS` instead.

## Multiple Sender Addresses

The SMTP login always stays the same:

```text
smtp@mail.example.com
```

Only the visible From address differs. In Thunderbird, create multiple identities for this; in FairEmail, create multiple identities/sender addresses with the same SMTP account.

## Typical Errors

| Message | Cause |
|---|---|
| `535 Authentication failed` | wrong username or wrong password |
| `530 Authentication required` | wrong port, TLS/auth not enabled |
| `554 Sender domain not allowed` | From/envelope domain not in the allowlist |
| `554 Sender domain not allowed` at DATA | From header does not match the allowlist |
| Certificate warning | wrong hostname; use `mail.example.com` or `mx.example.com` |

The visible From header must match the domain allowlist. The envelope sender is rewritten server-side to `bounces@example.org`; this is not visible to mail clients and does not need to be configured.
