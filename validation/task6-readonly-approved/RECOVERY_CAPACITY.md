# Current recovery capacity follow-up

This operator-only branch adds a read-only capacity check to application source
`67e7d8b4743ebca083f363d1fad0a53bebdcb04b`. It does not modify the reviewed API
application or the release candidate at that commit.

The existing recovery algorithm processes one consistent SQLite snapshot at a
time, caps total compressed archives at 4 GiB, and preserves an 8 GiB free-disk
reserve. Do not start it when the largest logical database plus archive cap and
reserve exceed free capacity. Do not reduce these bounds to make a preflight
pass. The next report quantifies the shortage and stats existing private
recovery files before selecting additional storage or verified off-host copies.
No files are removed by this check, including owned or obsolete scratch files.

Download both `vps-financial-inventory.py` and `vps-recovery-capacity.py` from the
same pinned operator commit, verify their SHA-256 checksums, and invoke the
capacity helper on the existing VPS. The first helper is also verified by hash
inside the capacity helper. Existing API/worker images, health, API limits and
isolated state volume must match. It compares the staging provider hostname
against available production container bindings without printing endpoints or
credentials and without sending any request to the provider. Absence of an
observed production match does not prove provider isolation.

The missing local ledger must not be initialized: `get_ledger` selects the
configured remote primary when both Supabase settings exist. That primary
requires its own verified recovery and ownership scope. Configuration presence
does not prove connectivity, isolation or backup coverage.

Private sizes and provider-binding results stay on the target or in the private
operator conversation. They are not committed as release evidence. This report
does not establish current backup, restore, authenticated acceptance, or Task 6
completion. It does not start or stop application services.
