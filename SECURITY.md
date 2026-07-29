# Security Policy

## Reporting a vulnerability

Do not open a public issue containing credentials, tokens, account numbers, supply-point identifiers, meter serial numbers, addresses, bills, transactions, or unredacted API responses.

Report security-sensitive findings privately to the repository maintainer through GitHub's private vulnerability reporting feature when available. Include the affected version or commit, reproduction steps, impact, and a redacted proof of concept.

## Sensitive data policy

The integration must not write passwords or access tokens to logs. Diagnostics must redact personal and billing identifiers. Test fixtures derived from real accounts must be irreversibly sanitized before being committed.

## Supported versions

Until the first stable release, only the latest commit on `main` is supported for security fixes.
