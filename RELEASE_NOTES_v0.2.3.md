# PokeRarity v0.2.3

This release adds automatic local database update checking while keeping the manual **Update Local DB** control.

## Highlights

- Checks the TCGdex card database revision automatically after launch.
- Rebuilds the local SQLite cache only when a newer revision is available.
- Shows **Last checked** and **Last updated** timestamps in the app.
- Keeps the existing local database available if the online check fails.
- Includes the corrected ENG/JAP rarity-symbol rendering from v0.2.2.

## Installation

1. Download and extract the release ZIP.
2. Run `setup.bat` once.
3. Run `run.bat` to start PokeRarity.

Python 3.12 is used for RapidOCR compatibility. Setup can install it through `winget` when available.
