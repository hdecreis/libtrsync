## Test/Example scripts

Those example will refer to ~/.config/libtrsync/testconfig.json to get phone number and pin.
They will store session parameters to ~/.config/libtrsync/session.json to avoid multiplying 2FA/AWS Token actions.
These paths can be overriden by $LIBTRSYNC_TESTCONFIG and $LIBTRSYNC_SESSION environment variables.

- smoke_fetch_all.py : provide 4 json files in out/ directory
    - accounts.json (listing security-cash account pairs)
    - assets.json (listing assets and their current snapshot)
    - transactions_raw.json (json extracted from API as-is)
    - transactions_dl.json (parsed transaction into a dual-legged format)

- smoke_track_asset.py : search an asset and subscribe to updates, show every update
