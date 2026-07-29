# ADR 0001: Use an OAuth public client

Status: accepted, pending OEJP application approval
Date: 2026-07-29

## Context

OEJP has deprecated email/password fields in `ObtainJSONWebTokenInput` and
recommends its authorization server. A Home Assistant custom integration cannot
keep a distributed client secret confidential.

## Decision

Use Authorization Code with PKCE S256 as the primary authentication method.
Support Device Authorization Grant through the same `AuthSession` boundary when
OEJP enables it. Never request or store the customer's OEJP password in the
public integration. Never distribute a client secret.

The client ID is committed only if OEJP authorizes a shared public client.
Otherwise, use Home Assistant Application Credentials. The legacy Kraken token
operation is isolated to a local read-only probe and removed from public setup
and runtime before alpha.

## Consequences

- OAuth application approval is a release blocker.
- Tokens remain local to each Home Assistant instance.
- Revocation and refresh failures use Home Assistant reauthentication.
- Tests need a deterministic local OAuth server and fake auth session.
- The authorization scheme and token rotation behavior remain configuration
  facts until OEJP confirms them.
