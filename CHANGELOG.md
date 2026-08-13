# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Sign-in is by email and password. The two OAuth methods are implemented and kept, but not
offered: Octopus Energy Japan replied on 2026-08-06 that it will issue no client to
individual customers.

## [Unreleased]

## [1.2.0b1] - 2026-08-13

A pre-release, so the customer who reported
[#93](https://github.com/SteveShin323/ha-octopus-energy-japan/issues/93) can check it against
a real time-of-use account. Nobody here is on one.

### Added

- **Cost for plans priced by time of day.** EVオクトパス, オール電化オクトパス and its
  サンシャイン variant, ソーラーオクトパス, and 動力オクトパス now produce a cost statistic, in all
  nine grid areas. Reported in
  [#93](https://github.com/SteveShin323/ha-octopus-energy-japan/issues/93) by a customer on the
  EV plan.

  Prices still come only from your own agreement. The hours each band covers are built in,
  because Octopus Energy Japan publishes them in its tariff documents but answers
  `KT-CT-1111 Unauthorized` for every API field that would return them — measured on two
  accounts, one of them on the EV plan itself. [docs/TOU_SCHEMES.md](docs/TOU_SCHEMES.md)
  records every hour and the document it came from.

  A time-of-use plan whose schedule is not in that table publishes no cost and says so, as
  does one whose agreement priced only some of its bands. Neither guesses.

- **The tariff shape in diagnostics now names the time-of-use scheme**, the grid operator
  code, and the band slots the provider returned, so a wrong cost can be traced without a
  customer's prices.

## [1.1.0] - 2026-08-11

Found on an installation with two logins — one for the current address, one from before a
move, whose contract has ended. Everything about collecting the data worked: both entries
loaded, the closed account's history was walked back seventeen months, and the cost
statistic was correctly absent where no agreement is in force. The names were the problem.

### Fixed

- **Energy Dashboard statistics ignored a name you gave a device.** The picker shows the
  statistic's name and nothing else, and the name was built from the name this integration
  generated rather than the one the user set. So an installation with two logins saw two
  identically named series there, even after renaming both devices to tell them apart. A
  name given by the user now wins. Statistic ids are unchanged, so no history moves.

### Added

- **A name for each connection**, under the integration's **Configure**. Devices are
  numbered within one entry, so two logins both produce `OEJP account 1` and
  `OEJP supply point 1-1`; naming them "home" and "old flat" makes the devices and their
  statistics `OEJP home account 1` and `OEJP old flat account 1`.

  Empty by default, which leaves every existing installation's names exactly as they are —
  renaming devices would rename entities that automations refer to. An ordinal cannot be
  made unique across entries without becoming unstable: it would depend on how many other
  entries exist, so removing one would renumber the rest.

### Changed

- **Configure** no longer refuses to open when nothing has ended. It aborted with "no
  historical resources", which is why an installation with two logins had no way to reach a
  setting that would have told them apart.

## [1.0.1] - 2026-08-06

### Changed

- the README is rewritten for readability. Same facts, shorter sentences, and the reasoning
  moved out of the reader's way: setup is now four steps and a line saying there is nothing
  else to enter, with the explanation of why browser sign-in is missing folded into a
  collapsed section. Sections a user scans — entities, updates, limitations, removal — lead
  with what happens rather than with why. The Japanese guide follows the same shape.
- `PRIVACY.md` notes that the OAuth column of its comparison applies only to a client you
  add yourself, since none is offered.

## [1.0.0] - 2026-08-06

Stable. Entity IDs, unique IDs, statistic IDs, and stored formats are settled from here:
changing one breaks somebody's automation or their Energy dashboard, so it now needs a
major version.

An OAuth application had been the last condition for this release. It was removed as a
condition rather than met. Asked directly, Octopus Energy Japan replied on 2026-08-06 that
OAuth is not supported in Japan and that it offers no API service to individual customers —
there is no client to wait for, and waiting would have meant never releasing.

### Changed

- **The two OAuth sign-in methods are no longer offered at setup.** They can only end at
  "the provider has not issued a client", and a choice that ends in an apology is worse
  than no choice: the user picks the method that sounds safer and comes away none the
  wiser. Email and password is what the menu offers, because it is what works.

  Nothing is deleted. Both implementations are complete and measured against the provider's
  own authorization server, and keeping them costs an empty constant. The menu asks whether
  an OAuth implementation exists rather than reading a flag, so they reappear by themselves
  the moment a client does — one issued to this integration, or one a user holds and adds
  through Application Credentials. Existing OAuth entries keep loading, and reauth and
  reconfigure still reach their steps.

## [0.9.7] - 2026-08-06

### Removed

- the supply-point screenshot. Its address was invented, but the ward in it was the real
  one — specific enough to narrow down where the account is, which is the thing the caption
  promised the image did not do. Removed rather than edited; it will be retaken with an
  address that names nowhere.

## [0.9.6] - 2026-08-06

### Added

- screenshots in the README, in both languages: the Energy Dashboard, a supply-point device,
  and an account device. Every figure in them is invented — they were taken from a throwaway
  Home Assistant holding synthetic statistics, with the device serial numbers replaced, so no
  real consumption, cost, account number, supply-point number, or address is published.

### Fixed

- **`tests/test_diagnostics.py` held a real account's number, supply-point number, bill
  amount, and product name.** They were the needles it searches the diagnostics report for,
  so the test that exists to keep those values out of a public place was the thing putting
  them there. Replaced with invented values of the same shape; the assertions are unchanged
  and still fail if any of them reaches the report.

## [0.9.5] - 2026-08-06

From a review of whether this integration copes with account shapes other than the one it
was built against.

### Added

- **A supply point whose plan has lapsed now says so.** Every consumption agreement it has is
  revoked or has ended with nothing to replace it — a plan switch, or a move-out with the
  entry still installed. The cost statistic stops either way, and until now it stopped in
  silence: the only other supply point that publishes no cost for a structural reason is an
  export-only one, which is silent on purpose, and that rule was silencing both. They are now
  told apart, and only the second is reported;
- a test that an account with no electricity supply points, or a property with none, parses
  rather than failing setup. A gas-only customer or one mid-move has that shape. It already
  worked; nothing pinned it, and the difference between "absent" and "empty" is one word in
  the parser.

### Changed

- `docs/API_CONTRACTS.md` now states which account shapes are measured and which are only
  reasoned about. One shape is verified against a real account — one account, one property,
  one electricity supply point, one agreement in force — and every other shape is exercised
  by hand-written payloads alone. That is how two of this project's eight "looks implemented,
  returns nothing" defects survived, so the table names them as unverified rather than
  letting `1.0.0` imply otherwise.

## [0.9.4] - 2026-08-06

### Changed

- **The OAuth client ships in the code, and nobody types anything.** It identifies this
  integration, not the customer, so one client serves every installation — asking each user
  to copy it from a README was a setup step that could only be got wrong, and Application
  Credentials additionally demands a client *secret* that a public client does not have.
  `oauth_metadata.OEJP_OAUTH_CLIENT_ID` is empty until Octopus Energy Japan issues one, and
  filling it in is the only change needed to make browser and device-code sign-in work.

  Registering it takes two places, both needed: `async_setup` for loading an existing entry
  after a restart, and the config flow itself, because a config-entry-only integration with
  no entries is never set up — measured against a real instance, a flow started there found
  the integration absent from `hass.config.components` and aborted for want of a client.

  Application Credentials stays as the override. The provider's discovery document advertises
  neither a `none` token-endpoint auth method nor `code_challenge_methods_supported`, so a
  public client is what its documentation describes rather than what its metadata proves; if
  the issued client turns out to be confidential, a credential added by hand is the way
  through, and it is offered alongside the shipped one.

  Verified end to end on a real Home Assistant with a placeholder client and no credential
  added: the browser method reached the provider's authorization page carrying the client id,
  PKCE `S256`, the fourteen read-only scopes and no `client_secret`, and the device method
  reached the provider and was refused for the placeholder — where both had previously
  stopped at "add an application credential first".

## [0.9.3] - 2026-08-06

### Fixed

- **An installation could not start once its stored access token had expired.** Setup failed
  with "Your accounts and supply points could not be read" and retried forever, with nothing
  in the log to say why. The provider reports an expired token as `KT-CT-1124`, carrying
  errorType `APPLICATION` — a type that says nothing about authentication, so the code is the
  only signal, and it was not in the table. Everything needed to recover was already there
  and simply never ran: the authenticated client refreshes and retries, but only for an
  authentication error, and the stored token is used without checking its age. So any restart
  more than a token lifetime after the last refresh was fatal, and the refresh token that
  would have fixed it in one call sat unused. Measured against a real account by replaying a
  stale token: `APPLICATION / KT-CT-1124 / "Signature of the JWT has expired."`

## [0.9.2] - 2026-08-06

Everything here was found by running 0.9.1 on a real Home Assistant and looking at what it
actually showed.

### Fixed

- **The setup screen displayed `[%key:common::config_flow::create_entry::authenticated%]`.**
  That syntax is a reference resolved by a build step in Home Assistant's own repository,
  which compiles `strings.json` into `translations/`. A custom integration ships its
  translations directly and never runs that step, so every reference reached the user
  verbatim — the setup screen and twelve OAuth abort messages, including two that email and
  password sign-in can reach. All thirteen are now written out in both languages, and a test
  fails if a reference ever returns. `strings.json` keeps them: it is the canonical source
  and is never displayed. The Japanese for one of them was written here rather than taken
  from Home Assistant, which ships no translation for it in any integration;
- **a supply point could show energy and no cost for up to half an hour after a restart.**
  The tariff arrives on a twelve-hour cadence and a cost series is only ever written by a
  statistics pass, which runs every thirty minutes; a price arriving did nothing by itself.
  It now provokes a pass. The readings have not moved, so the energy rows are left alone;
- **pressing Import full history blanked the calendar sensors it was meant to fill.** Today,
  this week and this month report a figure only when authoritative coverage reaches the
  snapshot's own timestamp, so that a period nobody has read says `unknown` rather than
  zero. A poll satisfies that by construction. Every other snapshot — a finished walk, a
  background window, a recorded failure — was dated with the wall clock, which claims an
  instant no window covers. Those snapshots now carry the last poll's date, which is what
  they actually know about;
- **fifteen sensors could go unavailable with no line anywhere saying why.** A poll where
  some directions still succeed does not raise, so Home Assistant logged nothing of its own
  and neither did this integration. A direction that stops being queryable now says so once,
  and says so again when it comes back;
- **an archive that could not be read priced its hours with no fuel-cost adjustment and no
  renewable levy.** The warning it logs promises those hours will be priced from the rate the
  provider reports now, and the code that would have done it could not be reached. Silently
  low, rather than missing.

### Changed

- `meters { serialNumber capacity }` is no longer requested. It was parsed into a model
  nothing ever read, and on the account measured the list is empty. `docs/API_CONTRACTS.md`
  records that, and that no field an account user can reach reports the contracted capacity:
  `contractedCapacity` and `contractedCapacityOld` are refused on both query paths, and
  `supplyDetails` answers null;
- the standing charge is documented as already resolved for the contract rather than as
  unestablished. `standingChargeUnitType` reads `YEN_AMPERE_DAY`, which describes how the
  charge is determined and not the unit of the number returned: on the account measured the
  figure equalled the published per-day amount for that account's contracted amperage, and
  the same amount appeared on its invoice. Using it as a daily amount, which is what this
  integration already did, is correct.

## [0.9.1] - 2026-08-06

Found by installing 0.9.0 in a real Home Assistant and pressing the button.

### Fixed

- **Import full history** appeared to do nothing. Starting the walk, and finishing it, both
  left every visible sign of it — the progress sensor, the diagnostics download, and the
  collected history itself — waiting for the next poll, which is up to half an hour away.
  A walk that has already run for hours should not owe the user another half hour before its
  readings reach the Energy Dashboard, and a button whose only feedback is half an hour late
  reads as broken. Both edges of the walk now publish immediately: the press, so that starting
  is visible, and whatever ends it — complete, refused, or failed.

  The windows in between still publish nothing, which is the part that matters for a walk of
  hundreds of them. Neither edge reaches Octopus Energy Japan: the snapshot is rebuilt from
  ledgers already written, so the whole walk still costs one statistics projection and two
  snapshots.

## [0.9.0] - 2026-08-06

Full history and prices that match the invoice. A supply point's readings can now be walked
back to the day supply began, stepped charges restart on the invoiced period rather than the
calendar month, and the two adjustments the provider only ever serves for the current period
are archived as they are observed so an earlier hour can still be priced with the figure that
applied to it. Still beta: `1.0.0` waits on a published OAuth client ID.

### Added

- **Import full history.** A button on each supply point's device page walks that supply point's
  readings back to where the account's history begins, instead of stopping at the two months a
  first install collects. Measured against a real account: intervals were present back to the day
  supply began, and the 1488-interval cap binds one response rather than a range — the legacy
  query stops 31 days back however wide the window, while the paginated generic query returned
  every interval asked for.

  Progress is a cursor on the checkpoint rather than a list of planned windows, so a five-year
  walk does not grow the checkpoint or make its saves quadratic, and a checkpoint written with
  this feature still loads on a build without it. The walk paces itself from what the provider
  says is left of the hourly allowance — a reading request costs a flat 17 points of 50,000, so
  one window every three seconds draws about a third of it, and below a 20,000-point reserve it
  waits for the reset. It ends where the account says billable supply began, and after three
  consecutive empty windows otherwise — which covers an account that cannot read that field and
  the gap between two supply periods for a customer who moved out and back in.

  A walked window stores its readings and nothing else: projecting statistics reads the whole
  ledger and rebuilding the snapshot re-reads every supply point, so one rebuild is requested when
  the walk finishes instead. Collected history reaches the Energy Dashboard statistics but not the
  calendar sensors, which only aggregate the current and previous month. A supply point whose
  readings arrive through the legacy path is refused rather than walked, because that path returns
  only the most recent 31 days and the walk would record a month as a complete history;
- the fuel-cost adjustment and renewable levy are archived as they are observed, so hours from
  an earlier period can be priced after Octopus Energy Japan has replaced them. It serves only
  the one in force, and every other input to a cost can be re-fetched — these two cannot, so
  they are the one thing this integration keeps a private copy of. Hours outside every archived
  period use the nearest archived value rather than nothing, which is what they used to get, and
  an archive that fails to load is left untouched rather than overwritten, because a save over
  it would destroy the only copy;
- stepped charges restart on the invoiced period instead of the Asia/Tokyo calendar month. The
  anchor is whichever the account reports that states the meter-reading schedule most directly:
  two consecutive scheduled reading dates that agree on a day one month apart, else the day
  billable supply began, else the calendar month as before. The rule was measured on one
  account with one closed invoice, so the derived anchor, the evidence behind it, and whether
  the provider's own reported reading day agrees are all in the diagnostics download;
- a repair message when the reported plan cannot be priced at all, with the reason, and one
  when a stored archive cannot be read. The diagnostics download gains a `tariffs` section
  carrying each plan's shape — product type, step count, rate generations, and what the
  standing charge is measured in. An absent cost statistic previously looked the same whether
  the plan could not be expressed or the integration was broken.

### Changed

- a statistics pass now reads the ledger from the current period boundary instead of from the
  first month ever collected, resuming each cumulative sum from the total the previous pass
  reached there. Reading everything cost one pass over the whole ledger for every correction,
  which grows without bound as history accumulates — and every refresh that adds an interval
  is a correction. Truncating is only sound on a period boundary, because that is where the
  cost formula's cumulative kWh restarts; a new `billing_period.py` names those boundaries.
  Sums are identical to a whole-ledger pass, and the pass falls back to reading everything
  whenever the boundary has no remembered total, which covers the first pass after a restart
  and any correction older than the two boundaries kept;
- a change to a price, a period boundary, or an archived adjustment republishes the whole cost
  series once. `dirty_from` limits publication to recent hours, so a corrected past would
  otherwise have been computed and then discarded before reaching the recorder. Energy rows are
  untouched, because a price does not move them.

### Fixed

- removing an entry now deletes the Energy Dashboard statistics it published. They were left
  in the recorder on purpose, so that removing the integration would not destroy an energy
  history — but the installation secret is deleted with the last entry and every statistic id
  is derived from it, so a re-added entry writes to new ids and the old rows can never be
  reached again. Worse, a statistic's display name comes from its device, whose name is
  ordinal and therefore identical after a re-install, so the Energy dashboard picker showed
  two series with the same name and no way to tell which one was live. When the last entry is
  removed, statistics left behind by an earlier removal are swept as well;
- accounts on a single-price plan got no cost statistic at all. The query asked for consumption
  charges only on the stepped product, and the parser then required a step boundary that a
  single-price charge does not have;
- an account that also exports could lose its consumption prices. The agreement in force was
  chosen by start date alone, so a later-starting export agreement — whose product carries
  generation credits and no consumption charges — won;
- a plan whose prices vary by time of day, or whose charges come from more than one grid
  operator, was treated as a step ladder and mispriced. Both are now refused with a recorded
  reason, as a charge in an unsupported unit already was;
- more than one published generation of rates became overlapping steps whose price depended on
  sort order rather than on the date. An hour is now priced with the generation in force then,
  and an hour no generation covers keeps the last price that had begun;
- an adjustment whose stated validity period could not be read was treated as open-ended, which
  made it apply to every moment in history.

## [0.8.1] - 2026-08-05

Migration handling, which the version scheme names as a condition for `1.0.0`. No user-visible
change: nothing in 0.8.0 is broken today, and this removes a hazard that a future schema
change would otherwise have created.

### Added

- migration handling, which the version scheme names as a condition for `1.0.0` and which
  did not exist. `async_migrate_entry` is defined: nothing needs migrating yet, but Home
  Assistant refuses to load an entry whose major version differs when no handler exists, so
  its absence — rather than the schema change — is what would have broken every entry at the
  next bump. An entry from a newer version is refused instead of being read with older code;
- a sync checkpoint that cannot be read is discarded and planning starts again, instead of
  raising. Raising the checkpoint's schema version would previously have left every
  installation permanently unable to synchronise, escapable only by deleting the entry and
  losing its history. A checkpoint is derived from the ledger, so discarding one costs
  re-reading those windows and loses no readings. `diagnostics` counts the discards.

## [0.8.0] - 2026-08-05

First release. Beta under this project's version scheme: everything the README describes is
implemented, covered by tests, and verified against a real account, but two of the three
sign-in methods cannot be completed until a client ID exists.

The entries below record what went into it.

### Added

- a choice of three sign-in methods at setup. **Email and password** works without an
  OAuth client ID, storing the credential because the provider's refresh token lasts
  seven days and renewing does not extend it. **Octopus Energy Japan account** and
  **device code** are OAuth and never see the password; the device code needs no
  redirect URI, so it does not require My Home Assistant and is the better of the two
  once a client ID exists. One OEJP login owns one config entry
  under either method, so an entry can be switched to OAuth in place — keeping its
  readings and statistics, and deleting the stored password. Removing such an entry
  deletes the local credential but cannot revoke the token, because Octopus Energy Japan does not let a
  customer invalidate a refresh token; it expires within seven days. See [ADR 0008](docs/adr/0008-password-authentication.md);
- read-only OEJP integration: OAuth with PKCE, account and supply-point discovery,
  generic and legacy reading providers with an explicit fallback policy;
- a persistent correction-aware interval ledger and Asia/Tokyo calendar aggregation;
- consumption, timestamp, delay, and lifecycle entities per supply point and
  direction, plus data-availability binary sensors;
- Energy Dashboard external statistics rebuilt from the ledger, so a reading corrected
  later also corrects every total that used it;
- optional account, contract, product, and billing summaries, with financial
  entities disabled by default;
- privacy-preserving diagnostics and four informational repair issues;
- icons for every entity, and English and Japanese translations;
- English and Japanese user documentation, a privacy statement, and a release
  process;
- a setup refusal when My Home Assistant is not enabled, because sign-in returns
  through `my.home-assistant.io`, the only redirect address submitted to OEJP for
  registration. Without it Home Assistant builds the instance's own callback URL
  and the user would meet the provider's unregistered-redirect error part-way
  through sign-in, with nothing naming this integration as the cause;
- **electricity cost on the Energy Dashboard.** The tariff is read from the agreement in
  force — the stepped prices with their kWh boundaries, the daily standing charge, the
  monthly fuel-cost adjustment and the annual renewable levy — and an hourly cost statistic
  is published for `stat_cost`. Nothing is entered by the user and no unit price is assumed.
  Home Assistant's own energy validator accepts it, which is asserted with a real recorder.
  Measured at 104% of one real bill, for the two reasons recorded in
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md);

### Corrected during development

Recorded because each was a real defect and the reasoning is worth keeping.

- documentation said to leave the Energy dashboard's cost field empty because a fixed
  price would be wrong. It is worse than wrong: Home Assistant builds a cost sensor only
  when the energy source is a valid entity id, and an external statistic is not one, so a
  price typed there is ignored rather than applied. The guides now say that, and a test
  pins the fact;
- device pages now show the account number and the supply-point number
  (供給地点特定番号) as their serial number. Hiding every provider identifier left a
  customer with two supply points unable to tell which device was which; entity IDs,
  names, states, attributes, and the diagnostics download still carry none;
- Energy Dashboard statistics were named after an identity digest, which is the only
  thing the Energy picker shows, so a household with two supply points could not tell
  them apart. They now take the supply-point device's name — `OEJP supply point 1-1
  Import energy` — and devices are created before the first refresh so the first
  publication already carries it. Home Assistant's own energy validator accepts both
  directions with no issues, which is now asserted rather than assumed;
- removing the integration left its stored readings, synchronisation checkpoints, and
  installation secret in Home Assistant's storage directory. Those files hold the
  account number, the supply-point number, and every collected interval, and both the
  privacy statement and the user guide said they were deleted. They now are, including
  every ledger month, while another entry's data and unrelated Home Assistant storage
  are untouched. The installation secret is shared between entries, so it goes only
  with the last one;
- the latest-reported-interval sensor declared `state_class: measurement` with the
  energy device class, which Home Assistant rejects. It logged "state class
  'measurement' which is impossible considering device class ('energy')" on every setup
  and pointed the user at this repository's issue tracker. It now carries no state
  class, because a 30-minute total replaced by the next one is not a running sum;
  long-term history comes from the published external statistics;
- the agreements query asked for `product.rates`, which an account user may not read.
  The resulting authorisation error propagated to the nearest nullable parent, so the
  whole product came back null and the **current plan name was lost** — to fetch a field
  the integration never publishes. Removing it resolves the plan name and clears the
  error, moving the agreements capability from partial to available;
- Energy Dashboard statistics are skipped with one warning when the recorder is not
  enabled, instead of raising `KeyError: recorder_instance`. `after_dependencies` orders
  the recorder but does not require it;
- `devices` and `registers` were queried without the `first` argument the
  provider's GraphQL guide requires on every paginated field. A conformance test
  now scans every shipped query, so a connection added later cannot omit it;
- user documentation claimed OEJP serves "roughly the last 30 days" of history.
  Measured on a real account, every interval since supply started is retrievable;
  the 31-day figure is a per-response cap of 1488 intervals that silently keeps the
  newest and drops the oldest. The scoped contract had this right and the user
  documentation contradicted it;
- an unreachable long-backfill planner was removed from `sync.py`, together with the
  background reason and priority nothing produced, and the two design documents that
  described it as an available feature. Why a long backfill is deliberately absent is
  recorded in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md);
- `OejpDeviceAuthSession` was a subclass with a docstring and no body, implying a
  capability that did not exist. Device-grant tokens come from the same token endpoint
  and refresh identically, so `OejpPkceAuthSession` serves them unchanged;
- the device-authorization endpoint was recorded as absent, which left the
  implemented RFC 8628 client unconstructible. The provider documents
  `/device-authorization/` and the live endpoint answers a POST with
  `invalid_request: Invalid client_id parameter value`, so it exists and only the
  client ID is missing. Its absence from the discovery document is a metadata gap.

### Provider behaviour confirmed against a real account

- the market name must carry a territory prefix. `JPN_ELECTRICITY` is accepted and
  `ELECTRICITY` is rejected with `KT-CT-4723`, which had silently disabled the whole
  generic reading path;
- a rejected credential is reported as `VALIDATION` with `KT-CT-1138`, not as an
  authentication error;
- one response returns at most 1488 intervals and drops the oldest beyond that silently,
  so every planned request stays well inside it;
- reading `version` switches from `DAILY` to `MONTHLY` when a billing period closes,
  which is the correction the ledger absorbs;
- provider monetary values are whole yen; and
- the generic reading API exposes no cost field at all.

### Deliberately absent

- **the per-interval cost figure Octopus Energy Japan returns.** Cost is published, but it
  is computed from the tariff. The provider's own figure applies one tier boundary where the
  tariff has two, combines the fuel-cost adjustment and the renewable levy into a single
  number, and cannot express the daily standing charge, so it does not add up to a billed
  total;
- any `kWh × unit price` estimate. One unit price cannot express tiered pricing, a daily
  standing charge, a monthly adjustment, an annual levy, and tax;
- a *current power* entity, because the API publishes 30-minute totals and an average
  presented as an instantaneous value would be wrong; and
- a *next meter reading* entity. The API exposes two such dates and both were measured in
  the past, disagreeing with the reading day on the same supply point.
