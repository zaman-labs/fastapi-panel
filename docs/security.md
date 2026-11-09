# Security

An admin panel can reach all your data, so its defaults are strict and don't depend on you turning anything on.

## What the panel handles

| Threat | Protection |
|---|---|
| Cross-site request forgery | A per-session token on every POST, including login, plus `Origin` and `Sec-Fetch-Site` checks |
| Session theft through scripts | `HttpOnly` cookie, and a Content-Security-Policy with no inline scripts or styles |
| Session tampering | HMAC-SHA256 signed cookie, compared in constant time |
| Sessions outliving access | Each request reloads the user. Deactivating them or changing their password ends the session |
| Session fixation | The session and CSRF token are replaced on login and logout |
| Password guessing | 5 failed attempts per username and IP, then a 15 minute lockout |
| Username discovery | One error message, and unknown usernames still cost a password check |
| Password theft from the database | scrypt with a per-password salt |
| Cross-site scripting | Autoescaped templates and the CSP. The audit log and flash messages are escaped too |
| Clickjacking | `frame-ancestors 'none'` and `X-Frame-Options: DENY`. The one exception is the add form opened by the **+** button, which only the panel itself may frame |
| Malicious uploads | Names are generated, never taken from the client. Extensions are allowlisted, SVG and HTML are refused, and images are checked against their contents |
| Uploaded files running as pages | Files come through signed, expiring links that only work for the staff member they were made for, with `nosniff` and a sandboxing CSP. Anything but a plain image downloads instead of opening |
| Path traversal | `LocalStorage` refuses any name that resolves outside its folder |
| SQL injection | All queries go through SQLAlchemy with bound parameters. `LIKE` wildcards are escaped |
| Mass assignment | Only fields on the form are written. Read-only fields and primary keys are ignored |
| Probing data through URLs | Sorting and filtering are allowlisted, unknown parameters are ignored |
| Rows from another parent or tenant | Inline rows must belong to the parent, and all access goes through `get_queryset` |
| Open redirects | `next` must stay inside the panel |
| Secrets on screen | Columns named like credentials are hidden unless listed explicitly |
| Secrets in logs | The audit log records field names, not values, unless you list a field in `log_values`. Fields named like credentials are refused |
| Scripts in rich text | `RichTextField` rebuilds submitted HTML from an allowlist, and only `http`, `https` and `mailto` links survive |
| Password reset abuse | Links use your configured `public_url`, never the `Host` header. They're signed, expire in an hour and stop working after use. Responses don't reveal which accounts exist, and requests are limited per username and IP |
| Spreadsheet formula injection | CSV cells starting with `=`, `+`, `-` or `@` are prefixed with `'` |
| Oversized requests | POST bodies have a size limit, counted as they arrive so chunked requests can't avoid it. Inline forms, bulk actions and CSV imports have row limits |
| Misconfiguration | It won't start without an auth backend, or with a secret under 32 characters. Plain text passwords are refused in `StaticAuth`, and `ModelAuth` won't guess whether someone is staff |

## What's left to you

**Use HTTPS.** Cookies get the `Secure` flag on HTTPS requests. Behind a proxy, make sure the app sees the real scheme, with `uvicorn --proxy-headers --forwarded-allow-ips=<proxy ip>`. Or force it with `secure_cookies=True`, which is a good idea in production anyway.

**Check the Host header.** `Origin` is compared to the request's host. If your proxy rewrites `Host`, POSTs get refused until you pass the public address: `trusted_origins=["https://admin.example.com"]`. Use FastAPI's `TrustedHostMiddleware` so that clients can't choose the host themselves.

**Forward the real client IP.** Login throttling goes by IP and username. Without `--proxy-headers` every client looks like your proxy, which means one attacker can lock out a username for everyone.

**Keep `secret_key` secret.** With it, anyone can forge a session. Load it from the environment, use a different one per environment, and rotate it if it might have leaked. Rotating logs everybody out.

**Turn on two-factor login.** `mount(..., two_factor=True)` asks for an authenticator code after the password, see [Two-factor authentication](auth-and-permissions.md#two-factor-authentication). Serving the panel on a VPN or restricting it by IP helps too.

**Sanitize rich text on your own pages.** The panel sanitizes `RichTextField` when it saves and displays it, but your site prints the column itself. Use `sanitize_html`, because rows can be written without going through the panel.

**Protect reset emails.** A reset link gives access to the account for an hour, so keep it out of your logs and error reports, and use a mail provider you trust.

**Mind the database user.** Actions run with your app's database credentials. Foreign key and unique rules still apply, and the panel shows a generic message if they fail.

## Known limits

- Throttling counters live in the process, so with `N` workers an attacker gets up to `5 × N` attempts per window. Put a shared rate limit in your proxy if that matters.
- Sessions are stored in the cookie, so a single session can't be revoked from the server. See [Sessions](auth-and-permissions.md#sessions).
- A file link keeps working for the person it was made for until it expires, even if they lose access to the record in the meantime.
- `FileField` doesn't look inside non-image files, so don't treat downloads as safe to open. Scan them yourself if strangers can upload.
- Authenticator secrets are stored unencrypted in `fastapi_panel_two_factor`, as with most TOTP setups. Anyone who can read that table can generate codes, though they still need the password.
- Audit entries are only written for changes made through the panel, not for direct database changes.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
