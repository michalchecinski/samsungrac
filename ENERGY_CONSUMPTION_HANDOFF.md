# Samsung AC usage history: findings and implementation handoff

Updated 18 November 2026. This document records the investigation and work from
this session for future agents and human maintainers. Observations below are
dated evidence, not a claim about the device's current state.

## Current outcome and scope

**Implemented and pushed: parsing the local usage database and exposing two raw
history counters. Accurate kWh and instantaneous W reporting is not implemented.**

The implementation revision is `aba2cb4` on `usage-file-readonly`. Its preceding
commit, `caae889`, added byte-preserving file retrieval and isolated background
polling. The starting revision was `c8f5c58`.

The branch was pushed to the user's fork:
<https://github.com/michalchecinski/samsungrac/tree/usage-file-readonly>.
The upstream project is <https://github.com/ismaelrivas/samsungrac>.
`origin` was verified as the user's fork; the authenticated GitHub account was
`michalchecinski` and had push/admin access. No upstream push, PR creation or merge
was performed by the agent in this session.

**The modified integration was not installed in Home Assistant.** No HA
configuration changes, debug logging changes, restart, AC activation or other
climate control requests were performed. A source review reported no blocking
issue for merging the raw-counter feature, with live HA verification explicitly
outstanding. That review did not approve a claim of accurate energy reporting.

## Authorization and instructions to preserve

- Keep HA inspection and AC requests read-only unless the user explicitly changes
  this constraint. Do not install the branch, change configuration, enable debug
  logging, restart HA or turn on the AC without explicit authorization.
- Keep credentials out of logs, fixtures, commits and reports. Do not request a
  token pasted into chat. Prefer an existing private credential source when
  authenticated access is required.
- Preserve unrelated local changes. The checkout was clean before this work.
- After every commit, push the committed branch to an authorized remote. This is
  the user's standing authorization; verify the destination, especially before
  pushing a fork branch. Do not push to the maintainer merely because it is origin.
- Clone external repositories under `/Users/michal/dev/<repo-name>`.
- For HA inspection, consult the Home Assistant best-practices skill and use the
  Home Assistant MCP. That skill was consulted during the original inspection.

## Device and integration observations

Read-only HA diagnostics on 17 November 2026 confirmed:

| Item | Observed value |
| --- | --- |
| Device description / firmware family | `TP6X_RAC_17K` |
| Climate IP device profile | `samsung_8888` |
| Connection engine | `raw` / `samsung_8888_raw` |
| Installed integration version | `10.0.1b10` |
| HA version | `2026.11.2` |
| HA Python version | `3.14.10` |
| Climate polling interval | 60 seconds, enabled |
| Integration logger level | WARNING |
| Advertised usage resource | `EnergyConsumption.saveLocation = /files/usage.db` |

Relevant device option tokens included `UsagesDB_254`, `OptionCode_56506` and
`ExtendOptionCode_1`. Their consumption-related meaning is unverified.
`UsagesDB_254` must not be presented as an energy value. `FilterTime_1410` is not
evidence of electrical consumption.

The SmartThings HVAC profile supports `powerConsumptionReport`, but the user's AC
did not advertise that capability. Retained HA logs contained no usage-file
payload. Before this work, `samsungrac.yaml` had neither an energy mapping nor a
usage-file reader. Its regular request is `GET /devices`.

Older Samsung port-2878 devices use `AC_ADD2_USEDPOWER` and a `/ 10` conversion in
`samsung_2878.yaml`. That is a different protocol and is not validation of a
port-8888 conversion factor.

## Original evidence and provenance

The user supplied the evidence at this local path; it is outside the repository:

```text
/Users/michal/Library/Mobile Documents/iCloud~md~obsidian/Documents/Second Brain/Smart home/Samsung AC energy investigation/
```

| File | Meaning |
| --- | --- |
| `README.md` | Investigation notes, updated after the usage download |
| `device-response.json` | Sanitized device status and advertised capabilities |
| `climate-ip.log` | Retained matching HA log records; no usage-file body |
| `usage-response.bin` | Original HTTP response body bytes |
| `usage-response.headers` | HTTP response status and headers, without request authorization |
| `usage-download.json` | Read-only GET transport metadata and completeness check |
| `usage.db` | Decoded SQLite database |
| `usage-inspection.json` | Earlier database inspection, schema and counter checks |

Initially, HA MCP redacted the AC host and token and offered no AC file-download
endpoint. No direct AC request was made by the implementation agent. The user
subsequently supplied the completed download artifacts. The agent independently
decoded those artifacts, compared the bytes and inspected SQLite in read-only
mode. Do not describe this as a live download test of the modified integration.

The supplied capture records authenticated `GET /files/usage.db` at
17 November 2026, 20:09:09 UTC / 21:09:09 Europe/Warsaw:

| Property | Observed value |
| --- | --- |
| HTTP status | 200 |
| Content-Type | `application/json` |
| Content-Length / received body bytes | 25944 / 25944 |
| Connection | `close` |
| TLS version | TLSv1 |
| Timed out | false |
| Entire advertised body received | true |
| Decoded database size | 19456 bytes |
| SQLite integrity check | `ok` |

SHA-256 of the original response body:

```text
c547cd59bc48c31c75a0a2503ef234445e3968e8a9b4ba0145ccb1f88adeef90
```

SHA-256 of the decoded database:

```text
d57a8bd99ad089572f1a178f6db6d1d71d3ae2b957c3d66f6c2d12714471f392
```

**The response body is plain base64 ASCII text containing an unencrypted SQLite 3
database. It is not JSON despite the Content-Type.** Strict base64 decoding of
the original response matched the supplied `usage.db` byte for byte. Do not call
JSON parsing first. This particular ASCII payload does not require binary body
support merely to download it; preserving original bytes still avoids corrupting
other file formats and permits exact verification.

## Verified schema and raw records

The database contains one application table and its primary-key index:

```sql
CREATE TABLE power_usage_table (date NUMERIC(12) primary key default 0, power_usage NUMERIC(14) default 0, running_time NUMERIC(14) default 0)
```

All 177 rows have integer values in the three columns. Ordered by date:

| Observation | `date` | `power_usage` | `running_time` |
| --- | --- | --- | --- |
| Earliest stored row | 2026052415 | 8817 | 18390 |
| Latest stored row | 2026111014 | 9690 | 19770 |
| Difference over this history | — | 873 raw counts | 1380 raw counts |

Within this single capture, neither counter decreases. Adjacent power-counter
increments range from 0 to 13; runtime-counter increments range from 0 to 10.
There are seven zero power increments and three zero runtime increments.
These are observations of raw numbers, not unit or rate validation.

Dates are consistent with `YYYYMMDDHH`; their timezone is unverified. The latest
stored row predates the download by seven days. Hour-shaped timestamps do not
prove hourly updates or live freshness. A nondecreasing snapshot does not prove
lifetime monotonicity, rollover behavior or resets.

Neither the schema nor the capture specifies energy units, runtime units or a
conversion factor. The field name `power_usage` does not establish whether a count
means Wh, kWh or something else. `running_time` is a stored runtime counter of
unknown units; it is not an instantaneous-power measurement.

## Implementation map and behavior

| File | Work completed |
| --- | --- |
| `protocol_8888.py` | `SamsungFileResponse`, byte-preserving `request_file`, bounded explicit framing, shared socket locking, file-content/header logging avoided, Connection: close handling |
| `connection_raw.py` | `async_get_usage_file` using the current token and existing shared client/connection lock, without embedded commands or pairing |
| `usage_history.py` | Advertisement guard, strict base64/SQLite parsing, `UsageSnapshot`, attempt metadata, throttling and failure isolation |
| `coordinator.py` | Background usage reads after successful climate polls, existing network lock reused, listener updates after completion, cancellation on unload |
| `sensor.py` | Two diagnostic raw-counter sensors within the existing sensor platform and coordinator entity framework |
| `diagnostics.py` | Safe usage-attempt metadata and latest raw record for single and multiple coordinators |
| `README.md` | User-facing behavior, evidence, limitations and validation summary |
| `tests/test_usage_history.py` | Real parsing, transport, failures, throttling, malformed inputs, decreases and lock behavior |
| `tests/test_usage_sensors.py` | Sensor setup, raw values, classifications, availability and decreases |
| `tests/test_coordinator.py`, `tests/test_diagnostics.py` | Test doubles updated for the new reader and diagnostics |

Sensor display names are **Usage history power counter (raw)** and **Usage history
runtime counter (raw)**. They publish the latest saved `power_usage` and
`running_time` directly. They have no unit, device class or state class, and are
not suitable for the HA Energy dashboard. Attributes include the original record
date, `date_format: YYYYMMDDHH`, record count, `timezone_verified: false` and
`units_verified: false`. Neither totals nor reset offsets are fabricated.

The read is attempted at most once per 15 minutes, including failures, and only
for the raw engine with the exact advertised `/files/usage.db` path. That interval
is a conservative request policy, not a measured device update frequency.
Climate polling publishes its state without awaiting the background download.
The network request has a 10-second budget after obtaining the coordinator lock,
plus up to 2 seconds for socket cleanup. Climate commands share the lock and can
wait for that budget. SQLite parsing runs in a worker thread after releasing the
network lock. The database is deserialized in memory, with query-only mode,
trusted schema disabled, a progress-handler work limit and a 10000-row limit.
The production parser writes no database file.

File bodies are capped at 2 MiB and require one valid Content-Length. Unframed,
transfer-encoded, oversized, truncated and malformed responses are rejected;
the socket is closed when necessary to protect subsequent climate responses.
The existing text status reader retains its UTF-8 compatibility behavior.

Only the observed plain-base64 SQLite format and exact stored table SQL are
accepted. Equivalent schemas with different SQL spelling may be rejected as
unsupported. All records are validated; invalid historical rows also reject the
snapshot. Empty history provides no counter. Missing/empty files, unsupported
formats, malformed data, authorization errors and timeouts clear usage values
instead of reporting zero. Only the usage sensors become unavailable; these
failures do not clear climate state or initiate reauthentication.

Diagnostics expose attempt status and, where retained, HTTP status, byte count,
digest and latest raw record. They do not expose original response headers, the
full database or credentials. Parse failures currently replace attempt metadata,
so they do not retain every HTTP detail of a failed parse. Full original evidence
is in the external bundle, not HA diagnostics.

## Fixtures and test evidence

- `tests/fixtures/tp6x_device.txt`: sanitized real device advertisement.
- `tests/fixtures/tp6x_usage_base64.txt`: a fresh in-memory SQLite database rebuilt
  from the observed schema and all 177 real records, then base64 encoded. Rebuilding
  removes unused pages; there are no device identifiers or credentials. Its bytes
  and digest intentionally differ from the original response.
- Other binary, malformed and decreasing-counter cases are synthetic. They test
  handling, not actual device resets or verified scaling.

Validation of implementation revision `aba2cb4`:

| Check | Result |
| --- | --- |
| Usage discovery/parser/transport and raw-sensor tests | 55 passed |
| Focused affected-platform/coordinator/diagnostics tests | 126 passed |
| Full suite | 877 passed, 1 skipped, 5 failed |
| Ruff lint and formatting | Passed with repository-pinned Ruff 0.15.10 |
| YAML parsing and whitespace checks | Passed |
| Release ZIP generation and content checks | Passed; parser included, tests excluded |

The five full-suite failures all import the absent
`custom_components.climate_ip_tools.emulator_intesisbox`. The same five failures
were reproduced on an unchanged copy of starting revision `c8f5c58`:

```text
test_emulator_shared_state_and_broadcasting
test_emulator_delayed_response_and_queued_commands
test_emulator_start_helper_with_delay
test_emulator_ducted_ac_oscillation_and_loop_breaker
test_emulator_ducted_ac_direct_socket_loop_and_exit_on_auto
```

The skipped test requires a reachable local IntesisBox emulator. Do not claim the
full suite is entirely green or silently remove these tests to make it so.

The local test environment used Python 3.14.3, HA 2026.10.4,
`pytest-homeassistant-custom-component` 0.13.375 and Ruff 0.15.10. This differs from
the installed HA version recorded above. There was no live installed-branch test.

Temporary test resources created during the session, which may expire:

```text
/private/tmp/climate-ip-test-env/     isolated dependency environment
/private/tmp/climate-ip-layout/       custom_components/climate_ip symlink to this checkout
/private/tmp/climate-ip-baseline/     unchanged starting-source test layout
/private/tmp/climate-ip-raw-full-final.txt  final full-suite output
```

From this repository, the actual focused test command was:

```sh
PYTHONPATH=/private/tmp/climate-ip-layout /private/tmp/climate-ip-test-env/bin/python -m pytest tests/test_usage_history.py tests/test_usage_sensors.py tests/test_sensor.py tests/test_coordinator.py tests/test_protocol_8888.py tests/test_diagnostics.py -q --disable-warnings --timeout=30
```

Use `tests` instead of the listed modules for the full run. If the temporary
environment is gone, recreate a test environment and a parent directory containing
`custom_components/climate_ip` mapped to the checkout. Tests import that package
name; repository-root imports alone are insufficient. `pytest.ini` also contains
an environment-specific `/workspaces/ha_data/config` path.

The release command is `python .github/scripts/build_release_zip.py`. It produces
an ignored local `climate_ip.zip` containing integration files at the ZIP root.
Generation is not installation. Markdown handoff files are excluded by the
packager's extension allowlist.

## Related research

- [Upstream issue #10](https://github.com/ismaelrivas/samsungrac/issues/10) suggested
  downloading the usage file; its earlier SQLite suggestion was not proof of
  format or units. The supplied capture later established the format.
- [Upstream issue #5](https://github.com/ismaelrivas/samsungrac/issues/5) and
  [#6](https://github.com/ismaelrivas/samsungrac/issues/6) contain port-2878 filter
  runtime and energy-scaling discussion. Do not transfer those factors to 8888.
- [Samsung Washer Local source](https://github.com/perseus177/ha-samsung-washer-local/blob/b75ef1ee561de4426d2f150c81ce786169df2b3b/custom_components/samsung_washer_local/api.py)
  uses the same database structure for a different appliance and explicitly leaves
  its counter unit unverified. Its reported calibration and runtime behavior are
  not validation for this AC. The implementation here was written independently.

## Latest user discussion and unresolved work

The user wants W and kWh in Home Assistant. After learning that the implemented
sensors are raw, the user proposed assuming the values were kWh and W. The agent
explained that assigning `power_usage` a kWh unit with no scaling would make the
latest value 9690 assumed kWh, and that `running_time` cannot represent measured
instantaneous watts. Historical average watts could be calculated from energy
differences and elapsed time only if the energy conversion and timing assumptions
were explicitly chosen; it would still not be current power.

The user did not select either assumed-kWh-only or historical-average-W behavior,
and then requested this handoff. **No assumed units, conversion option, kWh sensor
or W sensor was implemented.** The proposal is not unit validation and is not a
settled choice of calculation. Preserve this distinction when resuming work.

Before accurate energy reporting can be completed:

1. Establish what `power_usage` measures and its conversion factor using
   device-specific documentation, verified decoding, a labelled Samsung reading
   or independent electricity metering. Separately establish runtime units if
   exposing a duration sensor.
2. Obtain further snapshots during naturally occurring use or authoritative
   documentation to establish update behavior, freshness, resets and rollover.
   Do not induce resets or turn on the AC without authorization.
3. Choose the HA energy unit and state class from that evidence. Do not assume
   `total_increasing` merely because one captured history never decreases.
4. For instantaneous W, find a genuine live measurement capability or use an
   appropriately installed external meter. History-derived average power must be
   named and documented as an average with a known interval.
5. With explicit authorization, perform an installed-branch HA smoke test for
   sensor registration, update/unavailability transitions, unload cancellation,
   authentication reuse and concurrent climate controls. Merge or deployment
   remains a separate action; this session performed neither.

If the user deliberately chooses an assumed conversion later, label it plainly
as assumed/unverified, record the exact factor and counter behavior assumptions,
and keep the verified raw counters available for comparison. Do not relabel the
runtime counter as measured power.
