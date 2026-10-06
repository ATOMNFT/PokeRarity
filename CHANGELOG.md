# Changelog

All notable changes to PokeRarity are documented here.

## v0.2.3

- Added an automatic local database check shortly after app launch.
- Automatically rebuilds the local cache only when the upstream TCGdex database revision changes.
- Kept the manual **Update Local DB** button.
- Added persistent **Last checked** and **Last updated** timestamps.
- Stores database-update metadata locally under `cache/database_meta.json`.
- Keeps the existing local database available if an automatic update check fails.

## v0.2.2

- Updated rarity-symbol rendering to follow the supplied ENG/JAP rarity chart.
- Corrected English Double Rare to two black stars.
- Added distinct renderings for Ultra Rare, Illustration Rare, Special Illustration Rare, Hyper Rare, ACE SPEC, Shiny Rare, Shiny Ultra Rare, Promo, and other mapped rarities.
- Japanese rarities display their corresponding abbreviations such as `RR`, `SR`, `AR`, `SAR`, and `UR`.

## v0.2.1

- Removed the Series field from card results.
- Fixed rarity-symbol canvas clipping.
- Moved the Rarity Symbol heading farther left inside its panel.

## v0.2.0

- Reworked rarity-symbol rendering so it no longer depends on Windows Unicode glyph support.
- Changed the card-image helper text to **Click to enlarge.**

## v0.1.11

- Switched card identification to a local-first SQLite database built from the public TCGdex cards repository.
- Added local ENG/JAP card data support and database update controls.
- Retained live lookup only as an optional fallback/enrichment path.

## Earlier development

Earlier 0.1.x builds established the Windows UI, RapidOCR integration, PokeScan parser/matching logic, crop and stop controls, progress indication, local logging, and card-image display.
